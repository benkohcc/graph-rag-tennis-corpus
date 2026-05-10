# Graph RAG POC

A working proof-of-concept comparing **Graph RAG** to **vector RAG** on a synthetic tennis equipment corpus, with end-to-end LLM-as-judge evaluation.

Graph RAG wins on every query category that requires reasoning across relationships — multi-hop, thematic, and adversarial — while matching vector RAG on simple single-fact lookups. Overall delta: **+0.70** (graph avg 2.95 vs vector avg 2.25 on a 0–3 scale).

## What you get

- 30 documents about a fictional tennis equipment ecosystem (manufacturers, athletes, suppliers, retailers) with deliberate multi-hop facts planted across them
- 20 labeled test queries spanning single-fact, two-hop, three-hop, thematic-global, adversarial, and uncertainty categories
- Full Python pipeline: chunk → extract → graph → communities → retrieve → answer → score
- Side-by-side comparison: vector RAG and Graph RAG run against the same queries, both scored by Claude as judge

Total runtime: ~5–8 minutes. Total cost: ~$1.20 in Anthropic API charges.

## Quick start

```bash
# 1. Install dependencies (creating a virtual env first is recommended)
python3 -m venv venv && source venv/bin/activate
pip install -e .
# OR: pip install -r requirements.txt

# 2. Set your API key
cp .env.example .env
# Edit .env, add: ANTHROPIC_API_KEY=sk-ant-...

# 3. Run it
make run
```

That's it. The Makefile handles the rest.

## Retrieval architecture — three modes

The system routes each query to one of three retrieval modes based on what the question needs.

### Mode 1: Combined (graph + vector)

**For:** Entity-anchored questions where the answer lives in the graph neighborhood or nearby documents. This is the default for most queries — single facts, multi-hop chains, disambiguation, uncertain/speculative questions.

**How it works:** Two independent retrieval arms run in parallel:
1. Graph arm — extract named entities from the query, resolve them to canonical graph nodes (exact match → alias lookup → embedding similarity), BFS-traverse outward up to 2 hops, collect all relationship edges and their source chunks
2. Vector arm — embed the query, return top-8 chunks by cosine similarity

The two chunk sets are deduplicated by chunk ID and labelled by provenance: `[Gn]` graph-only, `[GVn]` found by both (treated as strongest evidence since two independent methods converged), `[Vn]` vector-only. The merged context goes to the generation model in a single prompt.

**Why not just vector search?** Vector search retrieves chunks that are semantically similar to the query but can't follow relationship chains. A question like "which suppliers does Apex share with Volenti?" requires traversing `Apex→SOURCES_FROM→Toray` and `Volenti→SOURCES_FROM→Toray` to find the shared node — something vector search cannot do.

**Why not just graph traversal?** Extraction isn't perfect. Some facts live in chunks that weren't linked to graph edges during extraction. The vector arm catches those as a fallback. In this corpus, the key Hanil/Q1 2026 timeline facts (Q13) only reached the context window through the vector arm because no extracted edge pointed to that chunk from the Apex anchor.

**Disambiguation:** When a query entity matches multiple canonical graph nodes (e.g. "Apex" matches both Apex Racquets and Apex Mountaineering Equipment), the system detects this, traverses subgraphs from all matching nodes, and instructs the generation model to lead its answer with explicit disambiguation.

### Mode 2: Combined + Community

**For:** Entity-anchored questions that also require thematic synthesis — risk aggregation, multi-domain patterns, temporal comparisons, or industry-wide exposure analysis.

**How it works:** Runs the full combined retrieval (both arms, deduped chunks) then adds community summaries as a third context section. Community detection (Leiden algorithm) clusters tightly-connected graph nodes into thematic groups; each cluster is summarised by the LLM at indexing time. The top-k most relevant community summaries are retrieved by embedding similarity and injected alongside the graph/vector chunks.

**Why the community layer matters:** Individual graph edges encode specific facts. They don't synthesise cross-cutting patterns like "Apex faces simultaneous supply concentration risk, a competitor-owned supplier, an injured endorser, and channel pressure from a dominant retailer." Community summaries capture exactly this — the thematic picture that no single edge or chunk expresses on its own. This is what lifts thematic queries from score 1–2 to 3.

**Why not always use this mode?** Community retrieval adds latency and token cost. For a simple entity lookup like "who is the CEO?" the community context adds noise rather than signal — the generation model has to wade through industry-level synthesis to find one specific fact. The router reserves this mode for questions that genuinely need thematic synthesis.

### Mode 3: Global

**For:** Questions with no named entity anchor at all — pure thematic or industry-wide questions that don't name a specific company, person, product, or facility.

**How it works:** Skips entity extraction and graph traversal entirely. Embeds the query and retrieves the top-k community summaries directly. Those summaries go straight to the generation model for synthesis.

**When to use:** Rare in practice for a corpus this focused. "What are the main supply chain themes across the premium tennis industry?" has no entity to anchor on, so community retrieval is the only option. If a question names even one entity (a facility, a regulatory body, a company), combined_community is the better choice.

### Routing

A lightweight LLM call (Claude Haiku) classifies each query into one of the three modes before retrieval runs. The routing decision is binary at its core:

```
Does the query name any specific entity (company, person, product, facility, body)?
├── No  → GLOBAL
└── Yes → Does it also require thematic synthesis or multi-domain pattern matching?
           ├── Yes → COMBINED_COMMUNITY
           └── No  → COMBINED
```

The routing prompt includes explicit examples for each category and a hard rule: GLOBAL only fires when truly no named entity appears in the query. This prevents surface-level phrases like "the entire industry" from pulling entity-anchored questions into global mode.

## Entity resolution — four-stage pipeline

Raw LLM extraction produces many mention variants for the same real-world entity ("Toray", "Toray Carbon", "Toray Carbon Industries", "Toray Carbon Industries Ltd."). The resolution pipeline collapses these into canonical nodes:

**Stage 1 — Exact normalised match:** Strip corporate suffixes (Inc., Ltd., S.r.l., GmbH, etc.), collapse whitespace, lowercase. Group all mentions that normalise to the same string and pick the longest mention as the canonical name.

**Stage 2 — Alias-first pre-merge:** Each extracted entity carries an `aliases` field populated during extraction. For every alias, collect all canonical nodes that claim it and the node whose name it is. If all participants share the same entity type AND their best descriptions are semantically similar (cosine > 0.50), merge the whole group. The description veto is what keeps "Apex Racquets" (tennis brand) separate from "Apex Mountaineering Equipment" (climbing gear) despite both claiming the alias "Apex" — their descriptions are semantically far apart.

**Stage 3 — Fuzzy name-only merge:** Embed canonical names alone (not name+description). Merge pairs whose name embeddings score above 0.80 cosine similarity. Using name-only embeddings keeps true duplicates close together; adding description embeddings would pull identical entities apart when different chunks describe them differently.

**Stage 4 — Description divergence veto:** Before committing any fuzzy merge, check that the entities' best descriptions are semantically similar (cosine > 0.50). Blocks false positives where two genuinely different entities happen to have similar names.

## Generation prompt design

All three modes share a set of prompt rules that address failure patterns found during evaluation:

- **Provenance labels** (`[Gn]`, `[GVn]`, `[Vn]`, `[C]`) tell the model which retrieval method found each passage, so it can weight convergent evidence (`[GVn]`) most heavily
- **Uncertainty guard** — the model is explicitly instructed not to infer a definitive conclusion from indirect signals when the corpus withholds an answer. This fixed Q16 (will Volenti renew the contract?) where rich graph context was causing the model to over-synthesise a "likely won't renew" conclusion
- **Disambiguation instruction** — injected dynamically when multiple canonical graph nodes match the query entity, instructing the model to lead with disambiguation before providing detail

## Evaluation approach

### Why LLM-as-judge

The corpus is synthetic and the facts are controlled, so a reference-based automatic metric (exact match, ROUGE, BertScore) would work in principle. We chose LLM-as-judge instead for two reasons:

1. **Natural language answers don't have a single correct form.** "Toray supplies carbon fibre to Apex" and "Apex's primary carbon fibre source is Toray" are equivalent correct answers. Exact match would penalise both; an LLM judge handles paraphrase naturally.
2. **The interesting failure modes are qualitative.** Over-inference (the model concludes something the corpus doesn't state), hallucination, and incomplete coverage are easier to detect with a judge that reads the full answer than with a similarity metric that compares token distributions.

### Test set design

The 20 queries were written before any retrieval code was run, with deliberate coverage across failure modes:

| Category | N | What it tests |
|----------|---|---------------|
| `single_fact` | 5 | Direct lookups — baseline floor both systems should hit |
| `two_hop` | 6 | Facts that require traversing two document-spanning relationships |
| `three_hop` | 2 | Longer chains requiring three connected relationships across docs |
| `two_hop_adversarial` | 1 | Multi-hop with a plausible-but-wrong shortcut answer |
| `thematic_global` | 4 | Synthesis questions — no single chunk holds the answer |
| `adversarial_disambiguation` | 1 | Two entities share a name; correct answer requires identifying both |
| `uncertain` | 1 | Corpus deliberately withholds the answer; correct response is "unknown" |

Each query has a list of `expected_facts` — atomic claims the answer should cover — rather than a gold-standard string. The judge scores how many of those facts the answer addresses, not how similar it is to a reference phrasing.

### Scoring rubric

The judge (Claude Sonnet) receives the question, the list of expected facts, and the system's answer, then returns a 0–3 score:

| Score | Meaning |
|-------|---------|
| 0 | Wrong — misses the answer, fabricates, or contradicts expected facts |
| 1 | Partial — gets some facts but misses key points, or contains errors |
| 2 | Mostly correct — covers most facts, may miss minor nuance |
| 3 | Fully correct — covers all expected facts with appropriate grounding |

Both systems are scored against the same expected facts in the same prompt, so the judge's implicit standards are consistent across the comparison.

### What the judge doesn't catch

- **Verbosity:** A graph answer might use 400 words where 80 would do. The judge scores coverage, not concision.
- **Hallucinations outside the expected facts:** If a system fabricates a plausible-sounding additional fact that isn't in `expected_facts`, the judge may not penalise it unless it directly contradicts something.
- **Latency and cost:** The judge scores answer quality only. Cost and latency are measured separately (see [Cost and latency](#cost-and-latency)).

### Preventing judge drift

Running the judge as a single batch (not interleaved with generation) keeps the model's context clean between queries. Scoring both systems on the same query in the same call was considered but rejected — it risks the judge anchoring on the first answer when scoring the second. Instead, each answer is scored independently in its own API call.

## Results

```
AGGREGATE BY CATEGORY
Category                     N    Vec avg    Graph avg    Δ
--------------------------------------------------------------
single_fact                  5    3.00       3.00         +0.00
two_hop                      6    1.83       2.83         +1.00
three_hop                    2    2.00       3.00         +1.00
two_hop_adversarial          1    2.00       3.00         +1.00
thematic_global              4    1.75       3.00         +1.25
adversarial_disambiguation   1    3.00       3.00         +0.00
uncertain                    1    3.00       3.00         +0.00
--------------------------------------------------------------
OVERALL                     20    2.25       2.95         +0.70
```

Graph RAG wins or ties on every query. No losses. The pattern matches the design intent:
- **Single-fact:** tied (vector is sufficient for direct lookups)
- **Multi-hop, thematic, adversarial:** Graph RAG wins clearly — these require relationship traversal or thematic synthesis that vector search cannot provide

## Make targets

```
make help          List all available commands
make install       Install dependencies via pip install -e .
make check-env     Verify ANTHROPIC_API_KEY is set
make smoke         Run chunking only (no API calls, ~1 second)
make extract       Run extraction stage (~2-3 minutes, ~$0.30)
make graph         Run extraction + graph build
make communities   Run through community summarization
make run           Full evaluation with comparison table (~$1.20)
make clean         Wipe caches and results
make fresh         clean + run (full re-evaluation from scratch)
```

## Project layout

```
.
├── CLAUDE.md                ← Project context for Claude Code
├── README.md                ← You are here
├── [GRAPH_MAP.md](GRAPH_MAP.md)             ← Full map of the knowledge graph (148 nodes, 302 edges)
├── pyproject.toml           ← Python package metadata + deps
├── requirements.txt         ← Same deps in pip format
├── Makefile                 ← Common commands
├── .env.example             ← Template for the API key
├── .gitignore
│
├── tennis_corpus/           ← The synthetic corpus
│   ├── README.md            (← read this for the multi-hop chains explanation)
│   ├── queries.json         (← labeled test set)
│   └── docs/                (← 30 markdown documents)
│
└── graphrag_poc/            ← The Python implementation
    ├── README.md            (← architecture + tuning knobs)
    ├── config.py            ← All tuning knobs
    ├── chunking.py
    ├── extraction.py
    ├── graph_build.py       ← Four-stage entity resolution
    ├── edge_canonicalization.py
    ├── communities.py
    ├── retrieval.py         ← Three retrieval modes + shared arm logic
    ├── query_router.py      ← Three-way routing (combined / combined_community / global)
    ├── pipeline.py
    ├── evaluate.py
    ├── cache/               ← Pickle caches per stage (auto-created, gitignored)
    └── results/             ← Evaluation output (auto-created, gitignored)
```

## Using with Claude Code

This project ships with a `CLAUDE.md` that gives Claude Code the full context it needs — file layout, conventions, common tasks, debugging recipes, things to avoid. Just start Claude Code in this directory:

```bash
claude
```

## Cost and latency

### Full pipeline (`make run`)

| Stage | Model | Calls | Cost | Latency |
|-------|-------|-------|------|---------|
| Extraction | Sonnet | 48 | ~$0.30 | ~2–3 min |
| Community summaries | Sonnet | ~22 | ~$0.15 | ~1 min |
| Vector RAG queries | Sonnet | 20 | ~$0.20 | ~1 min |
| Graph RAG queries | Sonnet + Haiku | 60 | ~$0.45 | ~2 min |
| LLM-as-judge scoring | Sonnet | 40 | ~$0.20 | ~1 min |
| **Total** | | ~190 calls | **~$1.30** | **~5–8 min** |

### Re-run costs (with caching)

Every pipeline stage writes a pickle cache. Re-runs skip already-completed stages:

| What you change | Stages re-run | Cost | Latency |
|-----------------|---------------|------|---------|
| Nothing (pure re-run) | None (all cached) | ~$0.00 | <5 sec |
| Query set only | Queries + scoring | ~$0.55 | ~3 min |
| Config knobs (graph/retrieval) | Graph build + all downstream | ~$0.80 | ~4 min |
| Corpus documents | Full pipeline | ~$1.30 | ~5–8 min |

Delete the relevant `cache/*.pkl` file to force a stage to re-run. `make clean` wipes everything.

### Per-query latency breakdown (Graph RAG)

Each Graph RAG query involves up to four sequential LLM calls:

1. **Routing** (Haiku) — ~0.3 sec — classify query into combined / combined_community / global
2. **Entity extraction** (Haiku) — ~0.3 sec — extract named entities from query text
3. **Graph traversal** — <0.1 sec — BFS in-memory NetworkX, negligible
4. **Generation** (Sonnet) — ~3–5 sec — answer synthesis from merged context

Total per Graph RAG query: **~4–6 seconds**. Vector RAG is ~3–4 sec (no routing or entity extraction). Both systems run queries sequentially in the evaluation loop.

### Cost drivers

- **Extraction is the most expensive indexing stage** — each of the 30 documents is chunked into ~1.6 chunks on average, and each chunk gets a full extraction prompt. Prompt caching on the system prompt reduces this by ~40% on repeated extractions.
- **Community summarisation cost scales with graph density** — more extracted entities and edges → more communities → more summarisation calls. This corpus produces ~22 communities.
- **Graph RAG costs ~2× more per query than vector RAG** — the extra Haiku routing call + entity extraction call + wider context window for generation (graph edges + vector chunks + optional community summaries).
- **LLM-as-judge is a fixed overhead** — 2 scoring calls per query regardless of retrieval method.

## Cost optimisation — retaining accuracy

The pipeline currently prioritises correctness over cost. These are the highest-leverage changes to reduce spend without touching accuracy.

### 1. Downgrade extraction to Haiku (~$0.30 → ~$0.05)

Extraction is the largest single cost driver. The extraction prompt is heavily structured (JSON schema, entity/relationship types, alias instructions) — this kind of constrained structured output is exactly where Haiku performs close to Sonnet. A/B test: run `make extract` with `EXTRACTION_MODEL = "claude-haiku-4-5-20251001"` in `config.py` and inspect the resulting graph. If entity counts and relationship density are comparable, the saving is ~$0.25 per run with no pipeline changes.

### 2. Cache entity extraction across runs (already built, but often missed)

The extraction stage already caches per-chunk in `cache/extraction.pkl`. The single most impactful operational habit is **never running `make clean` unless you've changed the corpus or the extraction prompt**. Re-running queries, tuning retrieval config, or changing community parameters all reuse the extraction cache at zero marginal cost.

### 3. Collapse routing + entity extraction into one Haiku call (~$0.60 → ~$0.45 per 20 queries)

Currently each Graph RAG query makes two sequential Haiku calls: one to route (combined / combined_community / global) and one to extract named entities. These could be merged into a single prompt that returns both the routing decision and the extracted entities in one JSON response. Saves ~1 round-trip (~0.3 sec) and ~50% of the Haiku token cost per query.

### 4. Skip LLM-as-judge for cached queries

The judge runs 2 Sonnet calls per query (~$0.20 total) on every eval run, even when neither the query nor the answer changed. Hashing `(query_id, answer_text)` and caching judge scores would make iterative experiments (changing retrieval config, prompt tuning) nearly free on the evaluation side.

### 5. Prompt caching on community summaries

Community summaries are generated once and reused across all queries. They're good candidates for Anthropic's prompt caching (`cache_control: ephemeral`) — inject them as a cached prefix so the token cost is paid once per 5-minute cache TTL rather than once per query. At 3 community summaries × 20 queries, this could cut community-context token costs by ~80%.

### 6. Reduce `TOP_K_COMBINED_CHUNKS` selectively by routing mode

The combined mode currently retrieves 8 vector chunks regardless of query type. Single-fact queries rarely need more than 3–4. A routing-aware `k` (e.g. `k=4` for combined, `k=8` for combined_community) would shrink generation context — and therefore token cost — for the majority of queries that are simple lookups, while keeping the wider window for thematic queries where it matters.

### Cost floor

After applying all of the above, a rough floor for a full fresh run:

| Change | Saving |
|--------|--------|
| Haiku for extraction | ~$0.25 |
| Merged routing+extraction call | ~$0.08 |
| Cached judge scores (re-run) | ~$0.20 |
| Prompt caching on communities | ~$0.05 |
| **Total saving** | **~$0.58** |

Estimated cost after optimisation: **~$0.70** for a first run, **~$0.10** for a re-run that only re-evaluates queries.

## Troubleshooting

**"ANTHROPIC_API_KEY is not set"** → Either `cp .env.example .env` and edit it, or `export ANTHROPIC_API_KEY=sk-ant-...`

**"Module not found: anthropic"** → `pip install -e .` (or `pip install -r requirements.txt`)

**"Model not found" / 404 from API** → The model strings in `graphrag_poc/config.py` may need updating. Check current model names at https://docs.anthropic.com and update `EXTRACTION_MODEL`, `GENERATION_MODEL`, `ROUTING_MODEL`.

**Pipeline hangs or rate-limit errors** → The pipeline is synchronous and respects API rate limits naturally. If you hit limits, just re-run — the extraction stage caches per-chunk and will resume from where it left off.

**Results look bad** → Diagnostic order: single-fact failure means vector layer issue; multi-hop failure means extraction or entity resolution; thematic failure means community detection. See `CLAUDE.md` for detailed debugging recipes.

## License

MIT. The synthetic corpus is fictional — any resemblance to real entities is coincidental.
