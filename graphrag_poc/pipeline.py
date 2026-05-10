"""Indexing pipeline orchestration.

Runs the full pipeline: chunk → extract → resolve → graph → communities.
Each stage caches its output so re-runs are fast.
"""
from sentence_transformers import SentenceTransformer

from config import EMBEDDING_MODEL
from chunking import load_corpus, Chunk
from extraction import extract_all
from graph_build import build_graph
from communities import build_communities


def run_indexing_pipeline(use_cache: bool = True):
    """Run end-to-end indexing. Returns all artifacts needed for retrieval."""

    print("=" * 70)
    print("STAGE 1: Chunking")
    print("=" * 70)
    chunks = load_corpus()
    print(f"Loaded {len(chunks)} chunks from corpus")

    print("\n" + "=" * 70)
    print("STAGE 2: Entity & Relationship Extraction")
    print("=" * 70)
    entities, relationships = extract_all(chunks, use_cache=use_cache)

    print("\n" + "=" * 70)
    print("STAGE 3: Entity Resolution & Graph Construction")
    print("=" * 70)
    G, canonical_entities = build_graph(entities, relationships, use_cache=use_cache)

    print("\n" + "=" * 70)
    print("STAGE 4: Community Detection & Summarization")
    print("=" * 70)
    communities = build_communities(G, canonical_entities, use_cache=use_cache)

    print("\n" + "=" * 70)
    print("Indexing complete!")
    print("=" * 70)
    print(f"  Chunks: {len(chunks)}")
    print(f"  Canonical entities: {len(canonical_entities)}")
    print(f"  Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(f"  Communities: {len(communities)}")

    chunks_by_id = {c.chunk_id: c for c in chunks}
    embedder = SentenceTransformer(EMBEDDING_MODEL)

    return {
        "chunks": chunks,
        "chunks_by_id": chunks_by_id,
        "entities": entities,
        "relationships": relationships,
        "graph": G,
        "canonical_entities": canonical_entities,
        "communities": communities,
        "embedder": embedder,
    }


if __name__ == "__main__":
    run_indexing_pipeline()
