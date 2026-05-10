"""Evaluation harness: run all queries through vector RAG and Graph RAG, score, compare.

For each query in queries.json:
1. Run vector RAG → answer A
2. Run Graph RAG (routed) → answer B
3. Score both with LLM-as-judge against expected_facts
4. Aggregate by category
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List

from anthropic import Anthropic
from tqdm import tqdm

from config import (
    require_api_key, GENERATION_MODEL, ROUTING_MODEL,
    QUERIES_PATH, RESULTS_DIR,
)
from pipeline import run_indexing_pipeline
from retrieval import (
    VectorIndex, vector_rag_answer,
    graph_rag_global_answer,
    graph_rag_combined_answer,
    graph_rag_combined_community_answer,
)
from query_router import route_query


client = Anthropic(api_key=require_api_key())


@dataclass
class QueryResult:
    query_id: str
    query: str
    category: str
    expected_winner: str
    expected_facts: List[str]
    vector_rag_answer: str
    vector_rag_score: int
    graph_rag_answer: str
    graph_rag_method: str
    graph_rag_score: int
    routing_decision: str


JUDGE_PROMPT = """You are an evaluator scoring a system's answer to a factual question against a set of expected facts.

QUESTION: {query}

EXPECTED FACTS (the answer should ideally cover all of these):
{expected_facts}

SYSTEM ANSWER:
{answer}

Score the system answer on a 0-3 scale:
- 0 = WRONG: misses the answer entirely, fabricates incorrect facts, or contradicts expected facts
- 1 = PARTIAL: gets some expected facts but misses key points, contains some errors, or is incomplete
- 2 = MOSTLY CORRECT: covers most expected facts accurately, may miss minor nuance
- 3 = FULLY CORRECT: covers all expected facts accurately with appropriate nuance and grounding

Return ONLY a JSON object: {{"score": 0-3, "reasoning": "one-sentence explanation"}}"""


def score_answer(query: str, answer: str, expected_facts: List[str]) -> int:
    """LLM-as-judge scoring against expected facts."""
    facts_str = "\n".join(f"- {f}" for f in expected_facts)
    prompt = JUDGE_PROMPT.format(
        query=query, expected_facts=facts_str, answer=answer
    )
    try:
        response = client.messages.create(
            model=GENERATION_MODEL,  # use a strong model for judging
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("\n", 1)[0]
        data = json.loads(text)
        return int(data.get("score", 0))
    except Exception as e:
        print(f"  ⚠️  Scoring failed: {e}")
        return 0


def run_evaluation():
    """Full evaluation run."""
    # Index everything
    artifacts = run_indexing_pipeline()
    chunks = artifacts["chunks"]
    chunks_by_id = artifacts["chunks_by_id"]
    G = artifacts["graph"]
    canonical_entities = artifacts["canonical_entities"]
    communities = artifacts["communities"]
    embedder = artifacts["embedder"]

    # Build vector index
    print("\n" + "=" * 70)
    print("Building vector index for baseline...")
    print("=" * 70)
    vector_index = VectorIndex(chunks)

    # Load queries
    with open(QUERIES_PATH) as f:
        queries_data = json.load(f)
    queries = queries_data["queries"]

    print(f"\nRunning {len(queries)} queries through both systems...")

    def _run_graph_rag(query_text: str, mode: str):
        """Run the graph RAG branch for a given routing decision."""
        if mode == "global":
            gr_result = graph_rag_global_answer(query_text, communities, chunks_by_id, embedder)
        elif mode == "combined_community":
            gr_result = graph_rag_combined_community_answer(
                query_text, G, canonical_entities, communities, chunks_by_id, embedder, vector_index,
            )
        else:  # "combined" is the default for all entity-anchored queries
            gr_result = graph_rag_combined_answer(
                query_text, G, canonical_entities, chunks_by_id, embedder, vector_index,
            )
        return gr_result, mode

    results: List[QueryResult] = []
    # 3 workers: vector answer, routing+graph answer, and a spare for scoring overlap.
    # Kept deliberately small to avoid saturating the API rate limit.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for q in tqdm(queries, desc="Evaluating"):
            query_text = q["query"]
            expected_facts = q["expected_facts"]

            # Vector answer and routing decision run in parallel
            vec_future = pool.submit(vector_rag_answer, query_text, vector_index)
            route_future = pool.submit(route_query, query_text)

            # Graph answer starts as soon as routing resolves
            try:
                mode = route_future.result()
            except Exception as e:
                mode = "local"
            graph_future = pool.submit(_run_graph_rag, query_text, mode)

            try:
                vec_answer = vec_future.result().answer
            except Exception as e:
                vec_answer = f"(error: {e})"

            try:
                gr_result, mode = graph_future.result()
                graph_answer = gr_result.answer
                graph_method = gr_result.method
            except Exception as e:
                graph_answer = f"(error: {e})"
                graph_method = "error"
                mode = "error"

            # Score both answers in parallel
            vec_score_future = pool.submit(score_answer, query_text, vec_answer, expected_facts)
            graph_score_future = pool.submit(score_answer, query_text, graph_answer, expected_facts)
            vec_score = vec_score_future.result()
            graph_score = graph_score_future.result()

            results.append(QueryResult(
                query_id=q["id"],
                query=query_text,
                category=q["category"],
                expected_winner=q.get("expected_winner", ""),
                expected_facts=expected_facts,
                vector_rag_answer=vec_answer,
                vector_rag_score=vec_score,
                graph_rag_answer=graph_answer,
                graph_rag_method=graph_method,
                graph_rag_score=graph_score,
                routing_decision=mode,
            ))

            # Save incrementally in case of crash
            with open(RESULTS_DIR / "results.json", "w") as f:
                json.dump([asdict(r) for r in results], f, indent=2)

    # Print summary
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print(f"{'Query':<6} {'Category':<28} {'Vec':<5} {'Graph':<6} {'Method':<22} {'Routing':<12} {'Winner':<10}")
    print("-" * 100)
    for r in results:
        winner = "VECTOR" if r.vector_rag_score > r.graph_rag_score else (
            "GRAPH" if r.graph_rag_score > r.vector_rag_score else "TIE"
        )
        print(
            f"{r.query_id:<6} {r.category[:27]:<28} "
            f"{r.vector_rag_score:<5} {r.graph_rag_score:<6} "
            f"{r.graph_rag_method[:21]:<22} {r.routing_decision[:11]:<12} {winner:<10}"
        )

    # Aggregate by category
    print("\n" + "=" * 70)
    print("AGGREGATE BY CATEGORY")
    print("=" * 70)
    by_category: Dict[str, List[QueryResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    print(f"{'Category':<35} {'N':<4} {'Vec avg':<10} {'Graph avg':<10} {'Δ':<8}")
    print("-" * 75)
    for cat, rs in by_category.items():
        v_avg = sum(r.vector_rag_score for r in rs) / len(rs)
        g_avg = sum(r.graph_rag_score for r in rs) / len(rs)
        delta = g_avg - v_avg
        print(f"{cat:<35} {len(rs):<4} {v_avg:<10.2f} {g_avg:<10.2f} {delta:+.2f}")

    # Overall
    v_total = sum(r.vector_rag_score for r in results) / len(results)
    g_total = sum(r.graph_rag_score for r in results) / len(results)
    print("-" * 75)
    print(f"{'OVERALL':<35} {len(results):<4} {v_total:<10.2f} {g_total:<10.2f} {g_total - v_total:+.2f}")

    print(f"\nFull results saved to {RESULTS_DIR / 'results.json'}")
    return results


if __name__ == "__main__":
    run_evaluation()
