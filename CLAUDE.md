# Graph RAG POC — Claude Code Project Guide

This is a Graph RAG proof-of-concept that compares Graph RAG vs vector RAG side-by-side on a synthetic tennis equipment corpus. The full pipeline runs end-to-end: chunk → extract entities → build graph → detect communities → summarize → retrieve via three modes → score with LLM-as-judge.

## Project layout

```
.
├── CLAUDE.md                 ← You are here. Project context for Claude Code.
├── pyproject.toml            ← Python dependencies (preferred)
├── requirements.txt          ← Same deps in pip format (fallback)
├── .env.example              ← Template for the API key
├── Makefile                  ← Common commands (make install, make run, etc.)
├── tennis_corpus/            ← The synthetic corpus
│   ├── README.md
│   ├── queries.json          ← 20 labeled test queries
│   └── docs/                 ← 30 markdown documents
└── graphrag_poc/             ← The Python implementation
    ├── README.md
    ├── config.py             ← All tuning knobs
    ├── chunking.py
    ├── extraction.py
    ├── graph_build.py
    ├── communities.py
    ├── retrieval.py
    ├── query_router.py
    ├── pipeline.py
    ├── evaluate.py
    ├── cache/                ← Pickle caches per stage (auto-created)
    └── results/              ← Evaluation output (auto-created)
```

## What Claude Code should know

**Domain:** Information retrieval, knowledge graphs, RAG (retrieval-augmented generation). The code orchestrates Anthropic API calls (Claude Sonnet for extraction and generation, Claude Haiku for routing) and uses NetworkX for the graph layer.

**Coding conventions:**
- Python 3.10+ required
- Type hints on public functions, dataclasses for structured data
- Each module is independently runnable (`python module.py` runs a smoke test)
- Pickle-based caching at every pipeline stage so re-runs are instant
- No async/await — keep it simple, synchronous; the slowness is in API calls

**Important constraints:**
- All LLM calls go through the Anthropic SDK (`anthropic` package)
- Embeddings use `sentence-transformers` (local, no API)
- Graph is in-memory NetworkX — fine for the POC corpus, would migrate to Neo4j for production
- Community detection uses `graspologic`'s hierarchical Leiden, falls back to NetworkX Louvain

## How to run (the canonical workflow)

```bash
# 1. Install dependencies
make install
# OR: pip install -e .
# OR: pip install -r requirements.txt

# 2. Set the API key
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# 3. Run the full evaluation
make run
# OR: cd graphrag_poc && python evaluate.py
```

Total runtime: ~5–8 minutes. Total cost: ~$1.20 in API charges.

## Common tasks Claude Code may help with

### Running and debugging

- `make smoke` — fast sanity check (chunking only, no API calls)
- `make extract` — run only extraction stage (most expensive)
- `make graph` — run extraction + graph build
- `make communities` — full pipeline through community summarization
- `make run` — full evaluation with comparison table
- `make clean` — wipe all caches to force re-indexing

### Tuning experiments

If asked to "improve results" or "tune the system," the leverage points are in `graphrag_poc/config.py`:

- `ENTITY_RESOLUTION_THRESHOLD` (0.88) — controls how aggressively similar entities merge
- `GRAPH_HOP_DEPTH` (2) — how far to traverse from anchor entities
- `LEIDEN_MAX_CLUSTER_SIZE` (12) — community granularity
- `TOP_K_COMMUNITIES` (3) — how much thematic context to retrieve
- `MAX_SUBGRAPH_EDGES` (30) — cap on subgraph size

After changing config, delete the relevant cache file (`graphrag_poc/cache/<stage>.pkl`) and re-run.

### Debugging recipes

**"Multi-hop queries are failing"** → Inspect extraction quality first. Load `graphrag_poc/cache/extraction.pkl`, sample 30 entities and 30 relationships, look for missing entities or fabricated relationships in the relevant documents.

**"Communities look wrong"** → Load `graphrag_poc/cache/communities.pkl`, print each community's nodes and themes. If incoherent, the graph is too sparse (extraction problem) or Leiden parameters need tuning.

**"Costs are higher than expected"** → Check that prompt caching is enabled in `extraction.py` (the system prompt should have `cache_control`). Verify with the API response's `cache_read_input_tokens` field.

**"Specific query is failing"** → Run a single query through both systems with print statements:
```python
from pipeline import run_indexing_pipeline
from retrieval import VectorIndex, vector_rag_answer, graph_rag_hybrid_answer
artifacts = run_indexing_pipeline()
vector_index = VectorIndex(artifacts["chunks"])
query = "..."
print(vector_rag_answer(query, vector_index).answer)
print(graph_rag_hybrid_answer(query, artifacts["graph"], artifacts["canonical_entities"],
    artifacts["communities"], artifacts["chunks_by_id"], artifacts["embedder"]).answer)
```

### Extending the system

If asked to "add X feature" — most extensions should follow the existing module pattern:
- New retrieval mode → add a function in `retrieval.py`, register in `evaluate.py`
- New corpus → drop docs into `tennis_corpus/docs/`, run `make clean && make run`
- New eval metric → add to `evaluate.py`'s scoring function
- Production migration → swap `NetworkX → Neo4j` in `graph_build.py`, swap `numpy vector search → Qdrant/pgvector` in `retrieval.py`

## Things to NOT do

- **Don't introduce a vector database** for the POC. Numpy + sentence-transformers is the right choice at this scale; complexity isn't free.
- **Don't add async/concurrency** without measuring first. API rate limits are the bottleneck, not Python concurrency.
- **Don't restructure into a package** (with `src/`, `__init__.py`, etc.) unless asked. The flat module layout is intentional for inspectability during a POC.
- **Don't replace the synthetic corpus** unless explicitly asked — it's the eval substrate, and the labeled queries depend on the specific facts planted.
- **Don't commit** `.env`, `cache/*.pkl`, or `results/*.json` — they're either secrets, derived data, or experiment-specific.

## API key handling

The code reads `ANTHROPIC_API_KEY` from the environment. Two ways to set it:

1. **Local `.env` file** (preferred for development):
   ```bash
   cp .env.example .env
   # Edit .env, add: ANTHROPIC_API_KEY=sk-ant-...
   ```
   Then use `python-dotenv` or `set -a; source .env; set +a` before running.

2. **Direct export**:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

Make sure `.env` is in `.gitignore` (it already is).

## Reading order for new contributors

1. `README.md` (top-level) — what this is and how to run it
2. `tennis_corpus/README.md` — the corpus design and labeled query set
3. `graphrag_poc/README.md` — architecture and tuning knobs
4. `graphrag_poc/config.py` — all the parameters in one place
5. `graphrag_poc/pipeline.py` — orchestration entry point
6. `graphrag_poc/retrieval.py` — the most complex module, where the three retrieval modes live
