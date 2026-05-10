"""Edge-type canonicalization: cluster semantically similar rel_type strings.

Extraction produces diverse surface forms for logically equivalent edge types
(e.g., SUPPLIES / PROVIDES_TO / DELIVERS_TO / IS_SUPPLIER_OF). Clustering them
to a shared canonical name improves query-time graph traversal because a
relationship filter on "SUPPLIES" then also catches its paraphrases.

Design notes:
- We embed each rel_type with underscores replaced by spaces so the sentence
  transformer treats them as readable phrases, not opaque tokens.
- Greedy union-find clustering at cosine > 0.82 (empirically: this catches
  near-paraphrases without collapsing semantically distinct types).
- Directionality guard: types ending in _BY express the passive/inverse of
  their root (ACQUIRED vs ACQUIRED_BY). We never merge a _BY type with its
  active counterpart. Additionally, a hard stoplist of known antonym pairs is
  checked before any merge.
- Canonical name for each cluster = the most-frequent rel_type in raw data
  (frequency = most likely to be the intended "standard" form).
"""
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from extraction import Relationship

# Edge-type similarity threshold for merging
EDGE_SIM_THRESHOLD = 0.82

# Pairs of rel_types that must never be merged (they are semantic opposites).
# Stored as frozensets so order doesn't matter in lookups.
ANTONYM_PAIRS: List[frozenset] = [
    frozenset({"OWNS", "OWNED_BY"}),
    frozenset({"ACQUIRED", "ACQUIRED_BY"}),
    frozenset({"EMPLOYS", "EMPLOYED_BY"}),
    frozenset({"SUPPLIES", "SUPPLIED_BY"}),
    frozenset({"MANUFACTURES", "MANUFACTURED_BY"}),
    frozenset({"COMPETES_WITH", "COMPETED_AGAINST_BY"}),
]


def _is_passive(rel_type: str) -> bool:
    """Return True if rel_type ends in _BY, indicating a passive/inverse form."""
    return rel_type.endswith("_BY")


def _antonym_blocked(a: str, b: str) -> bool:
    """Return True if merging a and b is forbidden by the antonym stoplist or
    the _BY directionality heuristic."""
    # Hard stoplist check
    pair = frozenset({a, b})
    if pair in ANTONYM_PAIRS:
        return True
    # Directionality heuristic: never merge active and passive forms.
    # e.g., ACQUIRED and ACQUIRED_BY have the same root but opposite subjects.
    if _is_passive(a) != _is_passive(b):
        return True
    return False


def canonicalize_edge_types(
    relationships: List[Relationship],
    embedder: SentenceTransformer,
) -> Tuple[List[Relationship], Dict[str, str]]:
    """Cluster semantically equivalent rel_type strings and rewrite relationships.

    Args:
        relationships: All extracted relationships (will not be mutated).
        embedder: Sentence transformer used to embed rel_type strings.

    Returns:
        A new list of Relationship objects with rel_type rewritten to canonical
        names, and a mapping {original_type: canonical_type} for logging/debug.
    """
    if not relationships:
        return relationships, {}

    # Count occurrences of each raw rel_type (used to pick the canonical name)
    type_counts: Counter = Counter(r.rel_type for r in relationships)
    unique_types: List[str] = list(type_counts.keys())

    if len(unique_types) == 1:
        return relationships, {unique_types[0]: unique_types[0]}

    # Embed with underscores replaced by spaces for better semantic encoding
    texts = [t.replace("_", " ") for t in unique_types]
    embeddings = embedder.encode(texts, show_progress_bar=False)
    sim = cosine_similarity(embeddings)
    np.fill_diagonal(sim, 0)

    # Union-find over rel_type indices
    parent = list(range(len(unique_types)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Greedy merge: iterate upper triangle, merge if similar and not blocked
    for i in range(len(unique_types)):
        for j in range(i + 1, len(unique_types)):
            if sim[i, j] >= EDGE_SIM_THRESHOLD:
                if not _antonym_blocked(unique_types[i], unique_types[j]):
                    union(i, j)

    # Build clusters: root index → list of member type strings
    clusters: Dict[int, List[str]] = defaultdict(list)
    for i in range(len(unique_types)):
        clusters[find(i)].append(unique_types[i])

    # Canonical name = most frequent member in each cluster
    mapping: Dict[str, str] = {}
    for members in clusters.values():
        canonical = max(members, key=lambda t: type_counts[t])
        for m in members:
            mapping[m] = canonical

    # Rewrite relationships
    rewritten = [
        Relationship(
            source=r.source,
            target=r.target,
            rel_type=mapping.get(r.rel_type, r.rel_type),
            description=r.description,
            chunk_id=r.chunk_id,
        )
        for r in relationships
    ]

    return rewritten, mapping
