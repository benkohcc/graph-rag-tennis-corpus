"""Build the knowledge graph from extractions.

Four-stage entity resolution process:
1. Exact normalized name match (strips corporate suffixes, collapses whitespace)
2. Alias-first pre-merge: unambiguous aliases trigger merges without needing similarity
3. Fuzzy name-only embedding merge (threshold 0.80, name only — not name+description)
4. Description divergence veto: blocks merges where name similarity is high but
   descriptions are semantically far apart (e.g., tennis brand vs mountaineering brand)
"""
import pickle
import re
from collections import defaultdict
from typing import Dict, List, Tuple

import networkx as nx
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

from config import (
    CACHE_DIR, EMBEDDING_MODEL, ENTITY_RESOLUTION_THRESHOLD, ENTITY_DESC_VETO_THRESHOLD,
)
from extraction import Entity, Relationship
from edge_canonicalization import canonicalize_edge_types


def normalize_name(name: str) -> str:
    """Normalize entity name for exact matching."""
    name = name.lower().strip()
    # Remove common suffixes
    suffixes = [", inc.", ", inc", " inc.", " inc", ", llc", " llc",
                ", ltd.", " ltd", " s.r.l.", " s.r.l", " s.p.a.", " s.p.a",
                " co., ltd.", " co.", " gmbh", " ag"]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def resolve_entities(
    entities: List[Entity],
    embedder: SentenceTransformer,
) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """Cluster entity mentions into canonical entities using four resolution stages.

    Stage 1 — Exact normalized match: strips suffixes, collapses whitespace.
    Stage 2 — Alias-first pre-merge: if an alias of entity A exactly matches the
        normalized name of entity B, merge them — but only when the alias is
        unambiguous (owned by exactly one canonical). Shared aliases (e.g. "Apex"
        claimed by both "Apex Racquets" and "Apex Mountaineering Equipment") are
        skipped and left for the fuzzy pass.
    Stage 3 — Fuzzy name-only merge: embed entity names alone (not name+description)
        and merge at cosine > ENTITY_RESOLUTION_THRESHOLD (0.80). Name-only embeddings
        keep true duplicates close and divergent entities apart better than
        name+description embeddings, where differing descriptions pull embeddings apart.
    Stage 4 — Description divergence veto: before committing any fuzzy merge, check
        that the best descriptions of the two candidates are semantically similar
        (cosine > ENTITY_DESC_VETO_THRESHOLD). Blocks merges like "Apex Racquets"
        (tennis brand) and "Apex Mountaineering Equipment" (climbing gear) even if
        their names score above the name-similarity threshold.

    Returns:
        name_to_canonical: maps each raw entity name → canonical name
        canonical_entities: maps canonical name → {type, description, mention_count, chunk_ids}
    """
    print(f"Resolving {len(entities)} entity mentions...")

    # ── Stage 1: group by normalized name ────────────────────────────────────
    by_normalized: Dict[str, List[Entity]] = defaultdict(list)
    for e in entities:
        by_normalized[normalize_name(e.name)].append(e)

    # Pick the longest mention as canonical name for each normalized group
    initial_canonicals: List[Tuple[str, List[Entity]]] = []
    for norm, mentions in by_normalized.items():
        canonical_name = max((m.name for m in mentions), key=len)
        initial_canonicals.append((canonical_name, mentions))

    print(f"  After exact-match: {len(initial_canonicals)} candidate entities")

    # ── Stage 2: alias-first pre-merge ───────────────────────────────────────
    # For each canonical entity, collect the set of all aliases claimed by any
    # of its mention instances. Then build a bidirectional map:
    #   alias_norm → list of (canonical_idx, target_idx) pairs meaning
    #   "canonical_idx claims this alias, and target_idx has this as its name"
    # A merge fires when exactly one canonical claims the alias (unambiguous).
    norm_to_idx: Dict[str, int] = {
        normalize_name(name): i for i, (name, _) in enumerate(initial_canonicals)
    }

    # Union-find initialised early so alias merges feed into the fuzzy pass
    parent = list(range(len(initial_canonicals)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Collect type lookups (needed for the type-match guard in stage 3)
    type_by_idx: Dict[int, str] = {}
    for i, (_, mentions) in enumerate(initial_canonicals):
        type_counts: Dict[str, int] = defaultdict(int)
        for m in mentions:
            type_counts[m.entity_type] += 1
        type_by_idx[i] = max(type_counts, key=type_counts.get)

    # For each alias string that appears in any entity's aliases field, collect:
    #   - claimants: canonical indices whose mentions include this alias
    #   - target: the canonical index whose name IS this alias (if any)
    # A merge fires iff:
    #   - the alias resolves to exactly one named canonical (unambiguous target)
    #   - exactly one claimant (other than the target itself) claims the alias
    #     after accounting for current union-find state
    # This prevents "Apex" (claimed by both Apex Racquets AND Apex Mountaineering)
    # from triggering any merge, while "Hanil" (claimed only by Hanil Composites Co.)
    # correctly merges the short-form node into the full-name node.

    # Pre-compute best descriptions and their embeddings — needed for the Stage 2
    # description veto as well as the Stage 4 veto.
    canonical_names = [c[0] for c in initial_canonicals]
    desc_texts = []
    for _, mentions in initial_canonicals:
        best_desc = max((m.description for m in mentions), key=len, default="")
        desc_texts.append(best_desc[:300] if best_desc else "unknown entity")
    desc_embeddings = embedder.encode(desc_texts, show_progress_bar=False)

    def _desc_sim(i: int, j: int) -> float:
        return float(cosine_similarity(
            desc_embeddings[i].reshape(1, -1),
            desc_embeddings[j].reshape(1, -1),
        )[0, 0])

    # alias_norm → set of canonical indices that carry this alias in their mentions
    alias_to_claimants: Dict[str, set] = defaultdict(set)
    for i, (_, mentions) in enumerate(initial_canonicals):
        seen: set = set()
        for m in mentions:
            for alias in getattr(m, "aliases", []):
                alias_norm = normalize_name(alias)
                if alias_norm not in seen:
                    seen.add(alias_norm)
                    alias_to_claimants[alias_norm].add(i)

    alias_merge_count = 0
    alias_veto_count = 0
    for alias_norm, claimant_set in alias_to_claimants.items():
        if alias_norm not in norm_to_idx:
            continue
        target_idx = norm_to_idx[alias_norm]

        # Collect all participants (claimants + target) under current union-find roots.
        # Only merge roots that are same type AND whose descriptions are not divergent.
        # This handles Toray chains (all carbon fiber → merge) while blocking
        # Apex Racquets + Apex Mountaineering Equipment (tennis vs climbing → veto).
        all_participants = claimant_set | {target_idx}
        participant_types = {type_by_idx[find(p)] for p in all_participants}
        if len(participant_types) > 1:
            continue  # cross-type bridge — skip

        unique_roots = {find(p) for p in all_participants}
        if len(unique_roots) == 1:
            continue  # already all merged

        # Description veto: check all pairs of distinct roots.
        # If any pair is semantically far apart, skip the whole group.
        roots_list = list(unique_roots)
        # Pick a representative index for each root (the root itself if valid, else any member)
        root_repr: Dict[int, int] = {}
        for p in all_participants:
            r = find(p)
            if r not in root_repr:
                root_repr[r] = p
        vetoed = False
        for a_idx in range(len(roots_list)):
            for b_idx in range(a_idx + 1, len(roots_list)):
                ra, rb = roots_list[a_idx], roots_list[b_idx]
                sim = _desc_sim(root_repr[ra], root_repr[rb])
                if sim < ENTITY_DESC_VETO_THRESHOLD:
                    vetoed = True
                    break
            if vetoed:
                break

        if vetoed:
            alias_veto_count += 1
            continue

        for k in range(1, len(roots_list)):
            union(roots_list[0], roots_list[k])
        alias_merge_count += len(unique_roots) - 1

    print(f"  Alias-first merges: {alias_merge_count} (vetoed by description divergence: {alias_veto_count})")

    # ── Stage 3: fuzzy name-only merge ───────────────────────────────────────
    # Embed names only (not name+description) so differing per-chunk descriptions
    # don't pull embeddings of the same entity apart.
    name_embeddings = embedder.encode(canonical_names, show_progress_bar=False)

    name_sim = cosine_similarity(name_embeddings)
    np.fill_diagonal(name_sim, 0)

    fuzzy_merge_count = 0
    veto_count = 0
    for i in range(len(canonical_names)):
        for j in range(i + 1, len(canonical_names)):
            if find(i) == find(j):
                continue  # already merged via alias pass
            if name_sim[i, j] < ENTITY_RESOLUTION_THRESHOLD:
                continue
            if type_by_idx[i] != type_by_idx[j]:
                continue

            # ── Stage 4: description divergence veto ─────────────────────
            desc_sim = _desc_sim(i, j)
            if desc_sim < ENTITY_DESC_VETO_THRESHOLD:
                veto_count += 1
                continue

            union(i, j)
            fuzzy_merge_count += 1

    print(f"  Fuzzy merges: {fuzzy_merge_count} (vetoed by description divergence: {veto_count})")

    # ── Build final canonical mapping ─────────────────────────────────────────
    name_to_canonical: Dict[str, str] = {}
    canonical_entities: Dict[str, dict] = {}

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(canonical_names)):
        groups[find(i)].append(i)

    for _, member_indices in groups.items():
        group_canonicals = [canonical_names[i] for i in member_indices]
        chosen = max(group_canonicals, key=len)

        all_mentions: List[Entity] = []
        for idx in member_indices:
            all_mentions.extend(initial_canonicals[idx][1])

        for m in all_mentions:
            name_to_canonical[m.name] = chosen

        type_counts_final: Dict[str, int] = defaultdict(int)
        for m in all_mentions:
            type_counts_final[m.entity_type] += 1
        best_type = max(type_counts_final, key=type_counts_final.get)

        best_desc = max((m.description for m in all_mentions), key=len, default="")
        chunk_ids = list({m.chunk_id for m in all_mentions})

        all_aliases: set = {m.name for m in all_mentions if m.name != chosen}
        for m in all_mentions:
            all_aliases.update(getattr(m, "aliases", []))
        all_aliases.discard(chosen)

        canonical_entities[chosen] = {
            "name": chosen,
            "entity_type": best_type,
            "description": best_desc,
            "mention_count": len(all_mentions),
            "chunk_ids": chunk_ids,
            "aliases": list(all_aliases),
        }

    print(f"  Final canonical entities: {len(canonical_entities)}")
    return name_to_canonical, canonical_entities


def build_graph(
    entities: List[Entity],
    relationships: List[Relationship],
    use_cache: bool = True,
) -> Tuple[nx.MultiDiGraph, Dict[str, dict]]:
    """Build the resolved knowledge graph.

    Returns (G, canonical_entities). The edge-type mapping is saved in the
    pickle cache alongside these two artifacts for offline inspection.
    """
    cache_path = CACHE_DIR / "graph.pkl"
    if use_cache and cache_path.exists():
        print(f"Loading cached graph from {cache_path}")
        with open(cache_path, "rb") as f:
            data = pickle.load(f)
        # Support both old (2-tuple) and new (3-tuple) cache formats
        if len(data) == 3:
            return data[0], data[1]
        return data

    embedder = SentenceTransformer(EMBEDDING_MODEL)

    name_to_canonical, canonical_entities = resolve_entities(entities, embedder)

    # Canonicalize edge types before building the graph
    raw_type_count = len({r.rel_type for r in relationships})
    relationships, edge_type_mapping = canonicalize_edge_types(relationships, embedder)
    canonical_type_count = len({r.rel_type for r in relationships})
    print(f"Edge type canonicalization: {raw_type_count} raw types → {canonical_type_count} canonical types")

    # Show the largest clusters (those where multiple raw types merged)
    from collections import defaultdict as _dd
    reverse: dict = _dd(list)
    for raw, canon in edge_type_mapping.items():
        reverse[canon].append(raw)
    clusters_with_merges = sorted(
        [(canon, members) for canon, members in reverse.items() if len(members) > 1],
        key=lambda x: -len(x[1]),
    )
    if clusters_with_merges:
        print("  Largest merge clusters (canonical ← raw members):")
        for canon, members in clusters_with_merges[:5]:
            others = [m for m in members if m != canon]
            print(f"    {canon} ← {others}")

    G = nx.MultiDiGraph()

    # Add nodes
    for name, attrs in canonical_entities.items():
        G.add_node(name, **attrs)

    # Add edges, mapping endpoints to canonical names
    edge_count = 0
    skipped = 0
    for rel in relationships:
        src = name_to_canonical.get(rel.source)
        tgt = name_to_canonical.get(rel.target)
        if not src or not tgt:
            skipped += 1
            continue
        if src == tgt:
            continue  # Skip self-loops from extraction noise

        G.add_edge(
            src, tgt,
            rel_type=rel.rel_type,
            description=rel.description,
            chunk_id=rel.chunk_id,
        )
        edge_count += 1

    print(f"Graph built: {G.number_of_nodes()} nodes, {edge_count} edges (skipped {skipped} unresolved)")

    with open(cache_path, "wb") as f:
        pickle.dump((G, canonical_entities, edge_type_mapping), f)

    return G, canonical_entities


if __name__ == "__main__":
    from chunking import load_corpus
    from extraction import extract_all

    chunks = load_corpus()
    entities, relationships = extract_all(chunks)
    G, canonical = build_graph(entities, relationships)

    # Print top entities by mention count
    print("\nTop 10 entities by mention count:")
    sorted_ents = sorted(canonical.values(), key=lambda x: -x["mention_count"])
    for e in sorted_ents[:10]:
        print(f"  {e['name']} ({e['entity_type']}): {e['mention_count']} mentions")
