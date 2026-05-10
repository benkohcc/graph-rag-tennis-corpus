"""Community detection and summarization.

Pipeline:
1. Run Leiden hierarchical clustering on the (undirected projection of the) graph
2. For each community, generate a structured summary via Claude
3. Each summary tracks the chunk_ids of its constituent edges (for citation later)
"""
import json
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set

import networkx as nx
import numpy as np
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

from config import (
    require_api_key, CACHE_DIR, EMBEDDING_MODEL,
    LEIDEN_RESOLUTION, LEIDEN_MAX_CLUSTER_SIZE,
    SUMMARY_MODEL, COMMUNITY_LEVEL_FOR_GLOBAL_QUERIES,
)


client = Anthropic(api_key=require_api_key())


@dataclass
class Community:
    community_id: int
    level: int
    nodes: List[str] = field(default_factory=list)
    summary: str = ""
    themes: List[str] = field(default_factory=list)
    key_claims: List[dict] = field(default_factory=list)  # [{claim, supporting_chunks}]
    chunk_ids: Set[str] = field(default_factory=set)
    summary_embedding: np.ndarray = None


SUMMARY_PROMPT = """You are summarizing a community of related entities and their relationships, drawn from a corpus of documents about a fictional tennis equipment industry.

ENTITIES IN THIS COMMUNITY:
{entities_block}

RELATIONSHIPS IN THIS COMMUNITY:
{relationships_block}

Produce a structured JSON summary with this exact schema:
{{
  "summary": "2-4 sentence overview of what binds this community together — the core theme, the central entities, and the most important shared context.",
  "themes": ["3-5 short theme labels capturing what this community is about"],
  "key_claims": [
    {{
      "claim": "A specific factual claim emerging from this community",
      "supporting_chunk_ids": ["chunk_id_1", "chunk_id_2"]
    }}
  ]
}}

Guidelines:
- Ground every claim in the relationships listed above. Do not introduce facts not supported by the data.
- Each key_claim's supporting_chunk_ids must come from the chunk_ids of the relationships shown.
- Aim for 4-8 key_claims that capture the most important content.
- Themes should be concise (2-5 words each).

Return ONLY valid JSON. No markdown, no commentary."""


def detect_communities(G: nx.MultiDiGraph, use_cache: bool = True) -> Dict[int, List[str]]:
    """Run hierarchical Leiden on the undirected projection.

    Returns: dict mapping community_id → list of node names.
    """
    cache_path = CACHE_DIR / "communities_raw.pkl"
    if use_cache and cache_path.exists():
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    print("Running Leiden community detection...")

    # Leiden requires an undirected graph
    undirected = nx.Graph()
    for src, tgt, data in G.edges(data=True):
        if undirected.has_edge(src, tgt):
            undirected[src][tgt]["weight"] += 1
        else:
            undirected.add_edge(src, tgt, weight=1)

    # Add isolated nodes
    for node in G.nodes():
        if node not in undirected:
            undirected.add_node(node)

    # Use graspologic's hierarchical_leiden
    try:
        from graspologic.partition import hierarchical_leiden
        clusters = hierarchical_leiden(
            undirected,
            max_cluster_size=LEIDEN_MAX_CLUSTER_SIZE,
            resolution=LEIDEN_RESOLUTION,
            random_seed=42,
        )
    except ImportError:
        # Fallback: use networkx's louvain_communities
        print("  graspologic unavailable, falling back to Louvain")
        from networkx.algorithms.community import louvain_communities
        partitions = louvain_communities(undirected, resolution=LEIDEN_RESOLUTION, seed=42)
        community_to_nodes = {i: list(p) for i, p in enumerate(partitions)}
        with open(cache_path, "wb") as f:
            pickle.dump(community_to_nodes, f)
        return community_to_nodes

    # graspologic returns list of (node, cluster, level, parent_cluster)
    # Take the leaf level (highest level number for each node)
    node_to_cluster: Dict[str, int] = {}
    for entry in clusters:
        node = entry.node
        cluster = entry.cluster
        # We want the leaf-level assignment
        if entry.level >= node_to_cluster.get(f"_level_{node}", -1):
            node_to_cluster[node] = cluster
            node_to_cluster[f"_level_{node}"] = entry.level

    # Strip the level metadata
    clean_node_to_cluster = {k: v for k, v in node_to_cluster.items() if not k.startswith("_level_")}

    community_to_nodes: Dict[int, List[str]] = defaultdict(list)
    for node, cluster in clean_node_to_cluster.items():
        community_to_nodes[cluster].append(node)

    print(f"  Found {len(community_to_nodes)} communities")
    print(f"  Sizes: {sorted([len(v) for v in community_to_nodes.values()], reverse=True)[:10]}")

    with open(cache_path, "wb") as f:
        pickle.dump(dict(community_to_nodes), f)

    return dict(community_to_nodes)


def summarize_community(
    community_id: int,
    nodes: List[str],
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
) -> Community:
    """Generate a structured summary for one community."""
    # Build entities block
    entity_lines = []
    for node in nodes:
        ent = canonical_entities.get(node)
        if not ent:
            continue
        entity_lines.append(
            f"- {ent['name']} ({ent['entity_type']}): {ent['description']}"
        )
    entities_block = "\n".join(entity_lines) if entity_lines else "(none)"

    # Build relationships block: edges where both endpoints are in this community
    node_set = set(nodes)
    rel_lines = []
    chunk_ids: Set[str] = set()
    for src, tgt, data in G.edges(data=True):
        if src in node_set and tgt in node_set:
            chunk_id = data.get("chunk_id", "")
            chunk_ids.add(chunk_id)
            rel_lines.append(
                f"- {src} --[{data.get('rel_type', 'RELATED')}]--> {tgt} "
                f"(source: {chunk_id}; context: {data.get('description', '')[:120]})"
            )
    relationships_block = "\n".join(rel_lines) if rel_lines else "(none)"

    if not entity_lines and not rel_lines:
        return Community(community_id=community_id, level=0, nodes=nodes)

    prompt = SUMMARY_PROMPT.format(
        entities_block=entities_block,
        relationships_block=relationships_block,
    )

    try:
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("\n", 1)[0]
        data = json.loads(text)

        return Community(
            community_id=community_id,
            level=0,
            nodes=nodes,
            summary=data.get("summary", ""),
            themes=data.get("themes", []),
            key_claims=data.get("key_claims", []),
            chunk_ids=chunk_ids,
        )
    except Exception as e:
        print(f"  ⚠️  Summary failed for community {community_id}: {e}")
        return Community(
            community_id=community_id,
            level=0,
            nodes=nodes,
            summary=f"Community of {len(nodes)} entities",
            chunk_ids=chunk_ids,
        )


def build_communities(
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
    use_cache: bool = True,
) -> List[Community]:
    """Run detection + summarization, embed summaries for retrieval."""
    cache_path = CACHE_DIR / "communities.pkl"
    if use_cache and cache_path.exists():
        print(f"Loading cached communities from {cache_path}")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    community_to_nodes = detect_communities(G, use_cache=use_cache)

    # Filter out trivial communities (singletons or pairs)
    significant_communities = {
        cid: nodes for cid, nodes in community_to_nodes.items()
        if len(nodes) >= 3
    }
    print(f"\nSummarizing {len(significant_communities)} significant communities...")

    communities: List[Community] = []
    for cid, nodes in tqdm(significant_communities.items(), desc="Summarizing"):
        community = summarize_community(cid, nodes, G, canonical_entities)
        communities.append(community)

    # Embed summaries for retrieval at query time
    embedder = SentenceTransformer(EMBEDDING_MODEL)
    summary_texts = [c.summary or " ".join(c.themes) or "empty" for c in communities]
    embeddings = embedder.encode(summary_texts, show_progress_bar=False)
    for c, emb in zip(communities, embeddings):
        c.summary_embedding = emb

    with open(cache_path, "wb") as f:
        pickle.dump(communities, f)

    return communities


if __name__ == "__main__":
    from chunking import load_corpus
    from extraction import extract_all
    from graph_build import build_graph

    chunks = load_corpus()
    entities, relationships = extract_all(chunks)
    G, canonical = build_graph(entities, relationships)
    communities = build_communities(G, canonical)

    print(f"\nTotal communities: {len(communities)}")
    for c in sorted(communities, key=lambda x: -len(x.nodes))[:5]:
        print(f"\n--- Community {c.community_id} ({len(c.nodes)} nodes) ---")
        print(f"Themes: {', '.join(c.themes)}")
        print(f"Summary: {c.summary[:300]}")
