"""Retrieval: vector RAG baseline + Graph RAG (local, global, hybrid, combined).

Vector RAG: embed query, top-k chunks by cosine similarity.

Graph RAG modes:
- LOCAL: anchor on entities mentioned in query, traverse subgraph, fetch source chunks
- GLOBAL: retrieve top-k community summaries, map-reduce over them
- HYBRID: do both, combine in the final prompt
- COMBINED: run graph local traversal AND vector search independently, dedupe by
  chunk_id, send single merged context to LLM — best of both without community overhead
"""
import json
import pickle
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx
import numpy as np
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from config import (
    require_api_key, CACHE_DIR, EMBEDDING_MODEL,
    EXTRACTION_MODEL, GENERATION_MODEL, ROUTING_MODEL,
    TOP_K_VECTOR_CHUNKS, TOP_K_COMBINED_CHUNKS, TOP_K_COMMUNITIES, GRAPH_HOP_DEPTH, MAX_SUBGRAPH_EDGES,
)
from chunking import Chunk
from communities import Community


client = Anthropic(api_key=require_api_key())


@dataclass
class RetrievalResult:
    answer: str
    method: str
    chunks_used: List[str]
    entities_used: List[str]
    communities_used: List[int]
    raw_context: str  # for debugging


# === VECTOR RAG ===

class VectorIndex:
    """Simple in-memory cosine-similarity vector index over chunks."""
    def __init__(self, chunks: List[Chunk]):
        self.chunks = chunks
        self.embedder = SentenceTransformer(EMBEDDING_MODEL)
        self.embeddings = self.embedder.encode(
            [c.content for c in chunks], show_progress_bar=False
        )

    def search(self, query: str, k: int = TOP_K_VECTOR_CHUNKS) -> List[Tuple[Chunk, float]]:
        q_emb = self.embedder.encode([query], show_progress_bar=False)
        sims = cosine_similarity(q_emb, self.embeddings).flatten()
        top_idx = np.argsort(sims)[-k:][::-1]
        return [(self.chunks[i], float(sims[i])) for i in top_idx]


def vector_rag_answer(query: str, vector_index: VectorIndex) -> RetrievalResult:
    """Pure vector RAG baseline."""
    results = vector_index.search(query, k=TOP_K_VECTOR_CHUNKS)

    context_parts = []
    chunks_used = []
    for i, (chunk, score) in enumerate(results):
        context_parts.append(f"[S{i+1}] (from {chunk.doc_id}): {chunk.content}")
        chunks_used.append(chunk.chunk_id)

    context = "\n\n".join(context_parts)

    prompt = f"""Answer the following question based on the source passages provided.
Cite source IDs (e.g., [S1]) for each claim. If the passages don't contain enough
information to answer, say so explicitly.

QUESTION: {query}

SOURCE PASSAGES:
{context}

ANSWER:"""

    response = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip()

    return RetrievalResult(
        answer=answer,
        method="vector_rag",
        chunks_used=chunks_used,
        entities_used=[],
        communities_used=[],
        raw_context=context,
    )


# === GRAPH RAG: ENTITY ANCHORING ===

ENTITY_EXTRACTION_PROMPT = """Extract the named entities mentioned or implied in this question. Return ONLY a JSON list of entity name strings, nothing else.

Examples:
"Who is Apex's CEO?" → ["Apex Racquets"]
"How does Volenti's Filamentrix acquisition affect Apex?" → ["Volenti Sport", "Filamentrix", "Apex Racquets"]
"What are the main supply chain risks?" → []

Question: {query}

Entity names (JSON list only):"""


def extract_query_entities(query: str) -> List[str]:
    """Identify entities mentioned in the query."""
    response = client.messages.create(
        model=ROUTING_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": ENTITY_EXTRACTION_PROMPT.format(query=query)}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("\n", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def resolve_query_entities_to_nodes(
    query_entities: List[str],
    canonical_entities: Dict[str, dict],
    embedder: SentenceTransformer,
) -> List[str]:
    """Match query entity strings to canonical graph nodes.

    Resolution order:
    1. Exact normalized name match
    2. Exact normalized match against any stored alias of a canonical node
    3. Embedding similarity fallback (threshold 0.6)
    """
    if not query_entities or not canonical_entities:
        return []

    from graph_build import normalize_name

    canonical_names = list(canonical_entities.keys())
    canonical_embeddings = embedder.encode(canonical_names, show_progress_bar=False)

    # Build alias → canonical name map for step 2
    alias_to_canonical: Dict[str, str] = {}
    for cn, attrs in canonical_entities.items():
        for alias in attrs.get("aliases", []):
            alias_norm = normalize_name(alias)
            if alias_norm not in alias_to_canonical:
                alias_to_canonical[alias_norm] = cn

    matched_nodes: List[str] = []
    for qe in query_entities:
        qn = normalize_name(qe)

        # Step 1: exact canonical name match
        matched = None
        for cn in canonical_names:
            if normalize_name(cn) == qn:
                matched = cn
                break

        # Step 2: alias match
        if matched is None and qn in alias_to_canonical:
            matched = alias_to_canonical[qn]

        # Step 3: embedding similarity fallback
        if matched is None:
            q_emb = embedder.encode([qe], show_progress_bar=False)
            sims = cosine_similarity(q_emb, canonical_embeddings).flatten()
            best_idx = int(np.argmax(sims))
            if sims[best_idx] > 0.6:
                matched = canonical_names[best_idx]

        if matched is not None:
            matched_nodes.append(matched)

    # Dedupe preserving order
    seen: set = set()
    result = []
    for n in matched_nodes:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result


def extract_subgraph(
    G: nx.MultiDiGraph,
    anchor_nodes: List[str],
    hop_depth: int = GRAPH_HOP_DEPTH,
    max_edges: int = MAX_SUBGRAPH_EDGES,
) -> Tuple[Set[str], List[Tuple[str, str, dict]]]:
    """BFS from anchors, collect nodes within hop_depth and edges among them."""
    if not anchor_nodes:
        return set(), []

    visited = set(anchor_nodes)
    frontier = set(anchor_nodes)

    for hop in range(hop_depth):
        next_frontier = set()
        for node in frontier:
            if node not in G:
                continue
            for neighbor in G.successors(node):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
            for neighbor in G.predecessors(node):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break

    # Collect all edges where both endpoints are in the visited set
    edges = []
    for src, tgt, data in G.edges(data=True):
        if src in visited and tgt in visited:
            edges.append((src, tgt, data))

    # Cap edges if too many; prioritize edges touching anchor nodes
    if len(edges) > max_edges:
        anchor_set = set(anchor_nodes)
        edges_with_anchor = [e for e in edges if e[0] in anchor_set or e[1] in anchor_set]
        edges_without_anchor = [e for e in edges if e[0] not in anchor_set and e[1] not in anchor_set]
        edges = edges_with_anchor + edges_without_anchor[: max(0, max_edges - len(edges_with_anchor))]

    return visited, edges


def _build_combined_graph_arms(
    query: str,
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
    chunks_by_id: Dict[str, Chunk],
    embedder: SentenceTransformer,
    vector_index: "VectorIndex",
    vector_k: int = TOP_K_COMBINED_CHUNKS,
) -> dict:
    """Shared retrieval logic for combined and combined+community modes.

    Returns a dict with:
      anchor_nodes, ambiguous_nodes, nodes, entity_lines, rel_lines,
      graph_chunk_ids, graph_only, both, vector_only, source_blocks,
      all_chunk_ids, disambig_note
    """
    from graph_build import normalize_name as _norm

    query_entities = extract_query_entities(query)
    anchor_nodes = resolve_query_entities_to_nodes(query_entities, canonical_entities, embedder)

    # Detect ambiguous entities (multiple canonical nodes match the same query string)
    ambiguous_nodes: List[str] = []
    for qe in query_entities:
        qn = _norm(qe)
        matches = [
            cn for cn, attrs in canonical_entities.items()
            if _norm(cn) == qn or qn in {_norm(a) for a in attrs.get("aliases", [])}
        ]
        if len(matches) > 1:
            ambiguous_nodes.extend(m for m in matches if m not in ambiguous_nodes)

    effective_anchors = list(dict.fromkeys(anchor_nodes + ambiguous_nodes))

    nodes: Set[str] = set()
    entity_lines: List[str] = []
    rel_lines: List[str] = []
    graph_chunk_ids: Set[str] = set()

    if effective_anchors:
        nodes, edges = extract_subgraph(G, effective_anchors)
        for node in nodes:
            ent = canonical_entities.get(node)
            if ent:
                entity_lines.append(f"- {ent['name']} ({ent['entity_type']}): {ent['description']}")
        for src, tgt, data in edges:
            cid = data.get("chunk_id", "")
            graph_chunk_ids.add(cid)
            rel_lines.append(f"- {src} --[{data.get('rel_type', 'RELATED')}]--> {tgt} [{cid}]")

    # Vector arm
    vec_results = vector_index.search(query, k=vector_k)
    vector_chunk_ids_ordered: List[str] = [chunk.chunk_id for chunk, _ in vec_results]

    # Classify chunks by which arm(s) retrieved them
    seen: Set[str] = set()
    graph_only: List[str] = []
    both: List[str] = []
    vector_only: List[str] = []

    for cid in sorted(graph_chunk_ids):
        if cid not in seen:
            seen.add(cid)
            (both if cid in vector_chunk_ids_ordered else graph_only).append(cid)

    for cid in vector_chunk_ids_ordered:
        if cid not in seen:
            seen.add(cid)
            vector_only.append(cid)

    # Build labelled passage blocks
    source_blocks: List[str] = []
    all_chunk_ids: List[str] = []

    for i, cid in enumerate(graph_only):
        chunk = chunks_by_id.get(cid)
        if chunk:
            source_blocks.append(f"[G{i+1}] (from {chunk.doc_id}): {chunk.content}")
            all_chunk_ids.append(cid)

    for i, cid in enumerate(both):
        chunk = chunks_by_id.get(cid)
        if chunk:
            source_blocks.append(f"[GV{i+1}] (from {chunk.doc_id}): {chunk.content}")
            all_chunk_ids.append(cid)

    for i, cid in enumerate(vector_only):
        chunk = chunks_by_id.get(cid)
        if chunk:
            source_blocks.append(f"[V{i+1}] (from {chunk.doc_id}): {chunk.content}")
            all_chunk_ids.append(cid)

    disambig_note = (
        f"\nNOTE: The knowledge graph contains {len(ambiguous_nodes)} distinct entities matching "
        f"the name(s) in this query: {', '.join(ambiguous_nodes)}. "
        "If the question is ambiguous (e.g. 'tell me about Apex'), begin your answer by explicitly "
        "identifying all matching entities and clarifying which is relevant before providing detail.\n"
        if ambiguous_nodes else ""
    )

    return dict(
        anchor_nodes=anchor_nodes,
        ambiguous_nodes=ambiguous_nodes,
        nodes=nodes,
        entity_lines=entity_lines,
        rel_lines=rel_lines,
        graph_chunk_ids=graph_chunk_ids,
        graph_only=graph_only,
        both=both,
        vector_only=vector_only,
        source_blocks=source_blocks,
        all_chunk_ids=all_chunk_ids,
        disambig_note=disambig_note,
    )


_COMBINED_RULES = """\
Rules:
1. Cite source IDs for every factual claim.
2. Weight [GVn] passages most heavily — corroborated by two independent retrieval methods.
3. If the passages explicitly state that something is unknown, undecided, or uncertain, \
reflect that uncertainty. Do NOT infer a conclusion from indirect signals (e.g. competitive \
dynamics, strategic logic) when the corpus withholds a definitive answer.
4. If key facts are missing from the passages, say so clearly rather than inferring."""


def graph_rag_local_answer(
    query: str,
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
    chunks_by_id: Dict[str, Chunk],
    embedder: SentenceTransformer,
    vector_index: Optional["VectorIndex"] = None,
    vector_fallback_k: int = 3,
) -> RetrievalResult:
    """Entity-anchored Graph RAG: traverse from query entities, fetch source chunks.

    When vector_index is provided, supplements graph-traversal chunks with up
    to vector_fallback_k vector-retrieved chunks that aren't already in the
    graph context. This guards against partial entity-anchoring failures where
    the graph has relevant information but the query entity didn't anchor onto
    the right node.
    """
    query_entities = extract_query_entities(query)
    anchor_nodes = resolve_query_entities_to_nodes(query_entities, canonical_entities, embedder)

    if not anchor_nodes:
        # Fallback to vector RAG if we can't anchor
        # (handled at the router level normally)
        return RetrievalResult(
            answer="(No entities found to anchor graph traversal — graph local mode not applicable)",
            method="graph_local_failed",
            chunks_used=[], entities_used=[], communities_used=[], raw_context="",
        )

    nodes, edges = extract_subgraph(G, anchor_nodes)

    # Build entities block
    entity_lines = []
    for node in nodes:
        ent = canonical_entities.get(node)
        if ent:
            entity_lines.append(f"- {ent['name']} ({ent['entity_type']}): {ent['description']}")

    # Build relationships block + collect source chunks
    chunk_ids_to_fetch: Set[str] = set()
    rel_lines = []
    for i, (src, tgt, data) in enumerate(edges):
        chunk_id = data.get("chunk_id", "")
        chunk_ids_to_fetch.add(chunk_id)
        rel_lines.append(
            f"- {src} --[{data.get('rel_type', 'RELATED')}]--> {tgt} [{chunk_id}]"
        )

    # Fetch graph-traversal source chunks
    graph_source_blocks = []
    graph_chunk_id_list = sorted(chunk_ids_to_fetch)
    for i, cid in enumerate(graph_chunk_id_list):
        chunk = chunks_by_id.get(cid)
        if chunk:
            graph_source_blocks.append(f"[G{i+1}] (from {chunk.doc_id}): {chunk.content}")

    # Vector fallback: add top-k semantically relevant chunks not already present
    vector_source_blocks = []
    vector_chunk_ids: List[str] = []
    if vector_index is not None:
        vec_results = vector_index.search(query, k=vector_fallback_k)
        vi = 1
        for chunk, _score in vec_results:
            if chunk.chunk_id not in chunk_ids_to_fetch:
                vector_source_blocks.append(f"[V{vi}] (from {chunk.doc_id}): {chunk.content}")
                vector_chunk_ids.append(chunk.chunk_id)
                vi += 1

    # Assemble context with clearly labelled sections
    context_parts = [
        "ENTITIES:\n" + "\n".join(entity_lines),
        "RELATIONSHIPS:\n" + "\n".join(rel_lines),
        "SOURCE PASSAGES (from graph traversal):\n" + "\n\n".join(graph_source_blocks),
    ]
    if vector_source_blocks:
        context_parts.append(
            "ADDITIONAL PASSAGES (from semantic search; may or may not be relevant):\n"
            + "\n\n".join(vector_source_blocks)
        )
    context = "\n\n".join(context_parts)

    vector_instruction = (
        "\nPrefer graph-traversal sources [Gn] when answering. "
        "Use semantic-search sources [Vn] only when they fill specific gaps the graph didn't cover. "
        "Cite source IDs."
        if vector_source_blocks else
        "\nCite source IDs [G1], [G2], etc. for each factual claim."
    )

    prompt = f"""Answer the following question using ONLY the knowledge graph and source passages below. Do not infer beyond what the passages explicitly state.

The ENTITIES section lists entities relevant to the question.
The RELATIONSHIPS section shows how they connect (each tagged with its source chunk).
The SOURCE PASSAGES section contains the original text supporting each relationship.
{vector_instruction}
If the relationships suggest a connection but the source passages don't fully support it, note that uncertainty.
If the passages explicitly state something is unknown or undecided, reflect that — do not infer a conclusion from indirect signals.

QUESTION: {query}

{context}

ANSWER:"""

    response = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip()

    all_chunk_ids = graph_chunk_id_list + vector_chunk_ids
    return RetrievalResult(
        answer=answer,
        method="graph_local",
        chunks_used=all_chunk_ids,
        entities_used=list(nodes),
        communities_used=[],
        raw_context=context,
    )


# === GRAPH RAG: GLOBAL (COMMUNITY SUMMARIES) ===

def retrieve_relevant_communities(
    query: str,
    communities: List[Community],
    embedder: SentenceTransformer,
    k: int = TOP_K_COMMUNITIES,
) -> List[Community]:
    """Embed query, return top-k communities by summary similarity."""
    if not communities:
        return []
    q_emb = embedder.encode([query], show_progress_bar=False)
    summary_embs = np.array([c.summary_embedding for c in communities])
    sims = cosine_similarity(q_emb, summary_embs).flatten()
    top_idx = np.argsort(sims)[-k:][::-1]
    return [communities[i] for i in top_idx]


def graph_rag_global_answer(
    query: str,
    communities: List[Community],
    chunks_by_id: Dict[str, Chunk],
    embedder: SentenceTransformer,
) -> RetrievalResult:
    """Map-reduce over community summaries with chunk grounding."""
    relevant = retrieve_relevant_communities(query, communities, embedder)

    if not relevant:
        return RetrievalResult(
            answer="(No relevant communities found.)",
            method="graph_global_failed",
            chunks_used=[], entities_used=[], communities_used=[], raw_context="",
        )

    # Collect supporting chunks from each community's key claims
    chunk_ids_to_fetch: Set[str] = set()
    community_blocks = []
    for c in relevant:
        claims_with_refs = []
        for claim in c.key_claims:
            supports = claim.get("supporting_chunk_ids", [])
            chunk_ids_to_fetch.update(supports)
            ref_str = ", ".join(supports) if supports else "no source"
            claims_with_refs.append(f"  - {claim.get('claim', '')} [{ref_str}]")
        community_blocks.append(
            f"[Community {c.community_id} | themes: {', '.join(c.themes)}]\n"
            f"Summary: {c.summary}\n"
            f"Key claims:\n" + "\n".join(claims_with_refs)
        )

    # Fetch chunks for grounding
    chunk_id_list = sorted(chunk_ids_to_fetch)
    source_blocks = []
    for i, cid in enumerate(chunk_id_list):
        chunk = chunks_by_id.get(cid)
        if chunk:
            source_blocks.append(f"[{cid}] (from {chunk.doc_id}): {chunk.content[:600]}")

    context = (
        "RELEVANT COMMUNITIES (clusters of related entities and claims):\n\n"
        + "\n\n".join(community_blocks)
        + "\n\nSOURCE PASSAGES (grounding for community claims):\n\n"
        + "\n\n".join(source_blocks)
    )

    prompt = f"""Answer the following question by synthesizing across multiple thematic communities.

The COMMUNITIES section shows distinct clusters of related entities and the key claims
emerging from each. The SOURCE PASSAGES section provides the underlying text grounding
those claims.

Provide a synthesized answer that:
1. Identifies the major themes from the communities
2. Cites specific claims with their source chunk IDs (e.g., [01_apex_10k_risk_factors_chunk_0])
3. Distinguishes between facts well-grounded in source passages vs. broader thematic claims
4. Notes any tensions or gaps in the available information

QUESTION: {query}

{context}

ANSWER:"""

    response = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip()

    return RetrievalResult(
        answer=answer,
        method="graph_global",
        chunks_used=chunk_id_list,
        entities_used=[],
        communities_used=[c.community_id for c in relevant],
        raw_context=context,
    )


# === GRAPH RAG: COMBINED (mode 1 — graph + vector, no community) ===

def graph_rag_combined_answer(
    query: str,
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
    chunks_by_id: Dict[str, Chunk],
    embedder: SentenceTransformer,
    vector_index: "VectorIndex",
    vector_k: int = TOP_K_COMBINED_CHUNKS,
) -> RetrievalResult:
    """Graph traversal + vector search, deduped. No community overhead.

    For entity-anchored questions where the answer lives in the graph neighborhood
    or nearby chunks. Handles disambiguation (multiple nodes matching query entity)
    and uncertainty-preserving generation automatically.
    """
    arms = _build_combined_graph_arms(
        query, G, canonical_entities, chunks_by_id, embedder, vector_index, vector_k
    )

    context_parts: List[str] = []
    if arms["entity_lines"]:
        context_parts.append("ENTITIES (from graph traversal):\n" + "\n".join(arms["entity_lines"]))
    if arms["rel_lines"]:
        context_parts.append("RELATIONSHIPS (from graph traversal):\n" + "\n".join(arms["rel_lines"]))
    context_parts.append(
        "SOURCE PASSAGES:\n"
        "[Gn] = graph traversal only  |  [GVn] = both methods (strongest evidence)  |  [Vn] = vector only\n\n"
        + "\n\n".join(arms["source_blocks"])
    )
    context = "\n\n".join(context_parts)

    prompt = f"""Answer the following question using ONLY the source passages below. Do not infer beyond what the passages explicitly state.
{arms["disambig_note"]}
Passages are labelled by retrieval method:
- [Gn]  = graph traversal
- [GVn] = BOTH graph traversal and semantic search — strongest evidence
- [Vn]  = semantic search only

{_COMBINED_RULES}

QUESTION: {query}

{context}

ANSWER:"""

    response = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return RetrievalResult(
        answer=response.content[0].text.strip(),
        method="graph_combined",
        chunks_used=arms["all_chunk_ids"],
        entities_used=list(arms["nodes"]),
        communities_used=[],
        raw_context=context,
    )


# === GRAPH RAG: COMBINED + COMMUNITY (mode 2 — graph + vector + community summaries) ===

def graph_rag_combined_community_answer(
    query: str,
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
    communities: List[Community],
    chunks_by_id: Dict[str, Chunk],
    embedder: SentenceTransformer,
    vector_index: "VectorIndex",
    vector_k: int = TOP_K_COMBINED_CHUNKS,
) -> RetrievalResult:
    """Graph traversal + vector search + community summaries.

    For entity-anchored questions that also need thematic synthesis across
    multiple risk categories, time periods, or industry patterns — things that
    require both the precise graph structure and the broader community-level picture.
    """
    arms = _build_combined_graph_arms(
        query, G, canonical_entities, chunks_by_id, embedder, vector_index, vector_k
    )

    # Community layer
    relevant_communities = retrieve_relevant_communities(query, communities, embedder)
    community_blocks: List[str] = []
    community_chunk_ids: Set[str] = set()
    for c in relevant_communities:
        claims = []
        for claim in c.key_claims:
            supports = claim.get("supporting_chunk_ids", [])
            community_chunk_ids.update(supports)
            claims.append(f"  - {claim.get('claim', '')} [{', '.join(supports) or 'no source'}]")
        community_blocks.append(
            f"[Community {c.community_id} | themes: {', '.join(c.themes)}]\n"
            f"Summary: {c.summary}\n"
            f"Claims:\n" + "\n".join(claims)
        )

    # Add any community-grounding chunks not already in the combined arms
    extra_community_blocks: List[str] = []
    all_seen = set(arms["all_chunk_ids"])
    for cid in sorted(community_chunk_ids):
        if cid not in all_seen:
            chunk = chunks_by_id.get(cid)
            if chunk:
                extra_community_blocks.append(f"[C] (from {chunk.doc_id}): {chunk.content[:600]}")
            all_seen.add(cid)

    context_parts: List[str] = []
    if arms["entity_lines"]:
        context_parts.append("ENTITIES (from graph traversal):\n" + "\n".join(arms["entity_lines"]))
    if arms["rel_lines"]:
        context_parts.append("RELATIONSHIPS (from graph traversal):\n" + "\n".join(arms["rel_lines"]))
    if community_blocks:
        context_parts.append("THEMATIC CONTEXT (community summaries):\n" + "\n\n".join(community_blocks))
    context_parts.append(
        "SOURCE PASSAGES:\n"
        "[Gn] = graph traversal only  |  [GVn] = both methods (strongest evidence)  |  [Vn] = vector only  |  [C] = community grounding\n\n"
        + "\n\n".join(arms["source_blocks"] + extra_community_blocks)
    )
    context = "\n\n".join(context_parts)

    all_chunk_ids = arms["all_chunk_ids"] + [c for c in sorted(community_chunk_ids) if c not in set(arms["all_chunk_ids"])]

    prompt = f"""Answer the following question using specific entity-level facts AND broader thematic context.
{arms["disambig_note"]}
Passages are labelled by retrieval method:
- [Gn]  = graph traversal
- [GVn] = BOTH graph traversal and semantic search — strongest evidence
- [Vn]  = semantic search only
- [C]   = community summary grounding

The THEMATIC CONTEXT section provides synthesized claims from clusters of related entities.
Use it to surface patterns and themes that individual edges don't capture.

{_COMBINED_RULES}

QUESTION: {query}

{context}

ANSWER:"""

    response = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return RetrievalResult(
        answer=response.content[0].text.strip(),
        method="graph_combined_community",
        chunks_used=all_chunk_ids,
        entities_used=list(arms["nodes"]),
        communities_used=[c.community_id for c in relevant_communities],
        raw_context=context,
    )


# Keep old names as aliases so existing call sites (evaluate.py fallback path) don't break
def graph_rag_hybrid_answer(
    query: str,
    G: nx.MultiDiGraph,
    canonical_entities: Dict[str, dict],
    communities: List[Community],
    chunks_by_id: Dict[str, Chunk],
    embedder: SentenceTransformer,
    vector_index: Optional["VectorIndex"] = None,
    vector_fallback_k: int = 3,
) -> RetrievalResult:
    """Alias for graph_rag_combined_community_answer (legacy call sites)."""
    vi = vector_index if vector_index is not None else None
    if vi is None:
        # No vector index — fall back to bare graph+community without vector arm
        # (shouldn't happen in practice since evaluate.py always passes vector_index)
        from retrieval import VectorIndex as _VI
        raise ValueError("graph_rag_hybrid_answer requires vector_index in the new architecture")
    return graph_rag_combined_community_answer(
        query, G, canonical_entities, communities, chunks_by_id, embedder, vi
    )
