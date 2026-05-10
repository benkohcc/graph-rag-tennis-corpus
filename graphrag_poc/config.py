"""Configuration for the Graph RAG POC.

Every tuning knob lives here so you can experiment without hunting through code.

Environment variables (loaded from .env if present):
- ANTHROPIC_API_KEY (required)
- EXTRACTION_MODEL (optional override)
- GENERATION_MODEL (optional override)
- ROUTING_MODEL (optional override)
"""
import os
from pathlib import Path

# Load .env file if present (no-op if python-dotenv isn't installed or .env missing)
try:
    from dotenv import load_dotenv
    # Look for .env in project root (one level up from graphrag_poc/)
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass


# === Paths ===
ROOT = Path(__file__).parent
CORPUS_DIR = ROOT.parent / "tennis_corpus" / "docs"
QUERIES_PATH = ROOT.parent / "tennis_corpus" / "queries.json"
CACHE_DIR = ROOT / "cache"
RESULTS_DIR = ROOT / "results"

CACHE_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)


# === Models ===
# Sonnet is used for high-quality extraction and final answer generation.
# Haiku is used for cheap operations: query routing, summarization map step.
# Override via env vars if you want to experiment.
EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "claude-sonnet-4-5")
GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "claude-sonnet-4-5")
ROUTING_MODEL = os.environ.get("ROUTING_MODEL", "claude-haiku-4-5")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-sonnet-4-5")

EMBEDDING_MODEL = "all-MiniLM-L6-v2"


# === Chunking ===
CHUNK_SIZE_TOKENS = 500   # rough; we split on paragraphs and pack
CHUNK_OVERLAP_TOKENS = 50


# === Entity resolution ===
# Fuzzy merge threshold on NAME-ONLY embeddings (not name+description).
# Lowered from 0.88 → 0.80 because name-only embeddings are tighter; the old value
# was calibrated for name+description embeddings which are pulled apart by diverging
# descriptions of the same entity across chunks.
ENTITY_RESOLUTION_THRESHOLD = 0.80

# Description divergence veto: if two candidates pass the name similarity threshold
# but their best descriptions are semantically further apart than this, the merge is
# blocked. Prevents "Apex Racquets" from merging with "Apex Mountaineering Equipment"
# even if name embeddings come close.
ENTITY_DESC_VETO_THRESHOLD = 0.50


# === Retrieval ===
TOP_K_VECTOR_CHUNKS = 5    # for vector RAG baseline
TOP_K_COMBINED_CHUNKS = 8  # vector arm in combined mode — wider net catches cross-doc facts
TOP_K_COMMUNITIES = 3      # how many community summaries to retrieve for global queries
GRAPH_HOP_DEPTH = 2        # how far to traverse from anchor entities
MAX_SUBGRAPH_EDGES = 30    # cap subgraph size before passing to LLM


# === Community detection ===
LEIDEN_RESOLUTION = 1.0
LEIDEN_MAX_CLUSTER_SIZE = 12
COMMUNITY_LEVEL_FOR_GLOBAL_QUERIES = 0  # 0 = leaf-level, higher = more abstract


# === API ===
# We don't raise here on missing key — that lets stages like chunking run
# without a key. Modules that actually call the API (extraction, retrieval, etc.)
# call require_api_key() before instantiating the Anthropic client.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def require_api_key() -> str:
    """Call this at the top of any module that uses the Anthropic API."""
    if not ANTHROPIC_API_KEY:
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable is not set.\n"
            "Either:\n"
            "  1. Copy .env.example to .env and add your key, OR\n"
            "  2. export ANTHROPIC_API_KEY=sk-ant-...\n"
            "Get a key at https://console.anthropic.com/settings/keys"
        )
    return ANTHROPIC_API_KEY


# Maximum concurrent API calls (avoid rate limits). Currently informational; the
# pipeline runs synchronously. Future async refactor would honor this.
MAX_CONCURRENT_CALLS = 5
