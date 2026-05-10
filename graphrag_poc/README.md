# Graph RAG POC — Tennis Equipment Corpus

End-to-end proof-of-concept Graph RAG implementation with side-by-side comparison against vector RAG. Runs against the synthetic tennis equipment corpus.

## What this does

Given the 30-document tennis corpus and the 20-query labeled test set:

1. **Chunks** all docs (~48 chunks at ~350 tokens)
2. **Extracts** entities and relationships via Claude (one call per chunk)
3. **Resolves** entities (normalize → exact match → embedding fuzzy merge)
4. **Builds** a NetworkX MultiDiGraph
5. **Detects communities** via hierarchical Leiden (graspologic)
6. **Summarizes each community** with claim-level chunk citations
7. **Indexes** vector RAG baseline alongside
8. **Routes** each query to local / global / hybrid Graph RAG
9. **Generates answers** from both systems
10. **Scores** both with Claude as judge against the labeled expected facts
11. **Aggregates** by category and prints a comparison table

## File structure

```
graphrag_poc/
  config.py          — All tuning knobs (models, thresholds, paths)
  chunking.py        — Paragraph-aware chunker
  extraction.py      — LLM entity/relationship extraction
  graph_build.py     — Entity resolution + NetworkX graph construction
  communities.py     — Leiden detection + community summarization
  retrieval.py       — Vector RAG + Graph RAG (local/global/hybrid)
  query_router.py    — LLM-based query routing
  pipeline.py        — Indexing orchestration
  evaluate.py        — Comparison harness with LLM-as-judge scoring
  cache/             — Pickle caches for each pipeline stage
  results/           — Evaluation output JSON
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Make sure the tennis corpus is at ../tennis_corpus/
ls ../tennis_corpus/docs/  # should show 30 .md files
ls ../tennis_corpus/queries.json  # should exist

# 3. Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

**Full evaluation (indexing + 20 queries):**
```bash
python evaluate.py
```

**Just indexing (pipeline stages, with caching):**
```bash
python pipeline.py
```

**Individual stages:**
```bash
python chunking.py     # preview chunks
python extraction.py   # test extraction on first 3 chunks
python graph_build.py  # full extraction + graph
python communities.py  # full pipeline through community summarization
```

## Caching

Every pipeline stage caches its output in `cache/`. To force re-extraction, delete the relevant pickle files:

```bash
rm cache/extraction.pkl    # re-extract entities/relationships
rm cache/graph.pkl         # re-build graph
rm cache/communities.pkl   # re-detect & re-summarize communities
```

## Cost estimate

For the 30-doc / 48-chunk corpus, full pipeline:

| Stage | Calls | Model | Approx cost |
|-------|-------|-------|-------------|
| Extraction | 48 | Sonnet | ~$0.30 |
| Community summaries | ~10-15 | Sonnet | ~$0.10 |
| Eval queries (vector RAG) | 20 | Sonnet | ~$0.20 |
| Eval queries (Graph RAG) | 20 + 20 routing + 20 entity-extract | Sonnet + Haiku | ~$0.40 |
| LLM-as-judge scoring | 40 | Sonnet | ~$0.20 |
| **Total** | ~180 calls | | **~$1.20** |

Prompt caching reduces extraction cost meaningfully (the long system prompt is identical across all 48 calls).

## Expected results

If everything is working correctly:

| Category | Vector RAG | Graph RAG | Notes |
|----------|-----------|-----------|-------|
| Single-fact (5 queries) | ~2.5 avg | ~2.5 avg | Should be roughly tied |
| Two-hop (7 queries) | ~1.5 avg | ~2.5 avg | Graph should win clearly |
| Three-hop (1 query) | ~0.5 avg | ~2.5 avg | Vector should fail |
| Thematic-global (4 queries) | ~1.0 avg | ~2.5 avg | Communities essential here |
| Adversarial (1 query) | ~1.5 | ~2.5 | Graph should disambiguate |
| Uncertain (1 query) | ~1.0 | ~2.5 | Graph should say "unresolved" |

If your numbers are dramatically different, debug in this order:
1. **Single-fact queries failing** → vector RAG issue (chunking, embeddings)
2. **Multi-hop failing** → extraction quality or entity resolution
3. **Thematic queries failing** → community detection or summarization
4. **Both systems doing poorly** → check that ANTHROPIC_API_KEY is set and chunks are loading

## Architecture decisions

**Why NetworkX?** In-memory, inspectable, fast for <50k nodes. Migrate to Neo4j when you outgrow this.

**Why graspologic for Leiden?** Hierarchical Leiden out of the box; falls back to NetworkX Louvain if unavailable.

**Why one extraction prompt per chunk?** Simpler to debug. Batched extraction is a future optimization.

**Why store chunk_ids on every edge?** So community summary claims can cite specific source chunks at query time. Without this, you can't ground community-derived claims.

**Why three retrieval modes?** Different queries need different evidence. Single facts → local. Themes → global. Mixed scope → hybrid.

## Tuning knobs to experiment with

In `config.py`:

- `ENTITY_RESOLUTION_THRESHOLD` (0.88) — too low merges distinct entities, too high leaves duplicates
- `GRAPH_HOP_DEPTH` (2) — increasing gives richer subgraphs but more noise
- `LEIDEN_MAX_CLUSTER_SIZE` (12) — controls community granularity
- `TOP_K_COMMUNITIES` (3) — how much thematic context to include
- `MAX_SUBGRAPH_EDGES` (30) — cap on subgraph size to keep prompts manageable

## Known limitations

- Synchronous extraction (slow on 48 chunks; ~3 minutes). Would batch in production.
- No incremental update path. Adding a doc means full re-run.
- Single-level community hierarchy. Microsoft GraphRAG uses multiple levels.
- Entity resolution is greedy union-find. Production needs better tie-breaking on close matches.
- No persistence beyond pickle. Would use Neo4j + Qdrant in production.

## Migrating to production

When this POC outgrows itself, swap layers without rewriting logic:

| POC layer | Production replacement |
|-----------|----------------------|
| NetworkX | Neo4j (same node/edge schema) |
| Numpy vector search | Qdrant or pgvector |
| Pickle caches | Persistent metadata DB |
| Synchronous extraction | Prefect/Temporal pipeline with concurrency |
| Single-level Leiden | Hierarchical Leiden with multi-level routing |
| Pickle community summaries | DB-backed with incremental update |

The extraction prompts, entity resolution logic, and retrieval composition carry over unchanged. Those are the parts where the actual learning lives.
