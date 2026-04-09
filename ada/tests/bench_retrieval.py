"""Retrieval benchmark — measures Ada's pgvector memory recall quality.

Inspired by LongMemEval: generates synthetic conversation episodes,
consolidates them into semantic memories, then measures how accurately
pgvector retrieval finds the right memory for a given query.

Metrics:
  R@1  — correct memory in top-1 result
  R@5  — correct memory in top-5 results
  R@10 — correct memory in top-10 results
  MRR  — mean reciprocal rank

Usage:
    python -m ada.tests.bench_retrieval [--episodes N] [--queries N]

Requires a running PostgreSQL instance with Ada's schema.
"""

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass

from ..config import AdaConfig
from ..memory.store import Store
from ..memory.embeddings import embed, embed_batch
from ..memory.models import Memory, Episode

log = logging.getLogger(__name__)


# ── Synthetic test data ──
# Each entry: (fact, query that should retrieve it, distractor queries)
EVAL_PAIRS = [
    {
        "fact": "Daniel prefers using Qwen models for local inference because of their strong reasoning at smaller parameter counts.",
        "query": "What models does Daniel like for local AI?",
        "type": "preference",
    },
    {
        "fact": "The APU orchestrator manages GPU VRAM using priority-based eviction with tiers: RESIDENT > VOICE > CODING > PLANNING.",
        "query": "How does GPU memory management work in Ada?",
        "type": "fact",
    },
    {
        "fact": "Ada's voice pipeline targets under 500ms end-to-end latency from speech input to speech output.",
        "query": "What is the latency target for voice responses?",
        "type": "fact",
    },
    {
        "fact": "The decision engine handles intent classification using 15 closed-set intents classified by Qwen 14B, with a keyword fallback chain.",
        "query": "How does intent classification work?",
        "type": "fact",
    },
    {
        "fact": "DeepSeek V3.2 is used as the cloud worker model with a budget of $100/month.",
        "query": "What cloud API does Ada use for coding tasks?",
        "type": "fact",
    },
    {
        "fact": "Memory consolidation runs every 30 minutes, extracting facts from unconsolidated episodes using LLM analysis.",
        "query": "How often does memory consolidation happen?",
        "type": "fact",
    },
    {
        "fact": "The Sentinel error recovery system has 4 validation gates: structural, technical, semantic, and attention.",
        "query": "What are the validation gates for error recovery?",
        "type": "fact",
    },
    {
        "fact": "UltraPlan uses a 3-pass planning engine (decompose, critique, synthesize) running Qwen 72B overnight.",
        "query": "How does overnight planning work?",
        "type": "fact",
    },
    {
        "fact": "The outbox pattern ensures restart-safe side effect delivery by writing state changes and events in the same transaction.",
        "query": "How does Ada handle crash recovery for dispatched events?",
        "type": "pattern",
    },
    {
        "fact": "Barge-in support in the voice pipeline allows the user to interrupt Ada mid-speech.",
        "query": "Can I interrupt Ada while she's talking?",
        "type": "fact",
    },
    {
        "fact": "The knowledge graph uses recursive CTEs for multi-hop traversal up to depth 4.",
        "query": "How does the entity relationship traversal work?",
        "type": "fact",
    },
    {
        "fact": "Task IDs follow the format tsk_YYYYMMDD_RANDOM6 for easy sorting and identification.",
        "query": "What format are task IDs in?",
        "type": "pattern",
    },
    {
        "fact": "Policy band 3 requires asking the user for permission before irreversible actions, budget overruns, and pivots.",
        "query": "When does Ada ask the user for permission?",
        "type": "decision",
    },
    {
        "fact": "The TRIBE critic scores response candidates on relevance, clarity, tone, and urgency match.",
        "query": "How does Ada evaluate response quality?",
        "type": "fact",
    },
    {
        "fact": "Whisper large-v3 is a RESIDENT model on GPU_1 that never gets evicted from VRAM.",
        "query": "Which model stays loaded permanently?",
        "type": "fact",
    },
    {
        "fact": "The noteboard uses WebSocket for bidirectional communication, broadcasting state updates to all connected clients.",
        "query": "How does the live UI communicate?",
        "type": "fact",
    },
    {
        "fact": "Daniel is building Ada as a voice-first AI operating system that runs entirely on local hardware with minimal cloud fallback.",
        "query": "What is the design philosophy behind Ada?",
        "type": "fact",
    },
    {
        "fact": "The context builder assembles prompts in parallel, querying active tasks, pending deliveries, episodes, and memories concurrently.",
        "query": "How is the LLM prompt assembled?",
        "type": "pattern",
    },
    {
        "fact": "Ada uses BAAI/bge-small-en-v1.5 for embeddings, producing 384-dimensional vectors on CPU.",
        "query": "What embedding model does Ada use?",
        "type": "fact",
    },
    {
        "fact": "The journal is an append-only audit log with rollback hints, enabling full replay of every decision Ada has made.",
        "query": "How are Ada's decisions audited?",
        "type": "fact",
    },
]

# Distractor memories to add noise
DISTRACTORS = [
    "The weather today is partly cloudy with a high of 72°F.",
    "Python 3.12 introduced several performance improvements to the interpreter.",
    "The Rust borrow checker prevents data races at compile time.",
    "Docker containers share the host kernel, unlike virtual machines.",
    "PostgreSQL supports JSON, JSONB, and array column types natively.",
    "The HTTP/2 protocol uses binary framing and multiplexing over a single connection.",
    "Git uses a directed acyclic graph to track commit history.",
    "The CAP theorem states a distributed system cannot provide consistency, availability, and partition tolerance simultaneously.",
    "Redis supports pub/sub messaging, sorted sets, and Lua scripting.",
    "WebAssembly enables near-native performance for web applications.",
    "The Linux kernel uses a completely fair scheduler for process management.",
    "GraphQL allows clients to request exactly the data they need.",
    "Kubernetes orchestrates containerized workloads across a cluster of machines.",
    "The Transformer architecture uses self-attention to process sequences in parallel.",
    "SQLite is the most deployed database engine in the world.",
    "TCP uses a three-way handshake to establish connections.",
    "The observer pattern decouples event producers from consumers.",
    "LLVM provides a modular compiler infrastructure for multiple languages.",
    "Bloom filters are space-efficient probabilistic data structures for set membership testing.",
    "The actor model uses message passing for concurrent computation.",
]


@dataclass
class BenchResult:
    total_queries: int
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    mrr: float
    avg_latency_ms: float
    memories_in_db: int


async def run_benchmark(
    config: AdaConfig,
    num_distractors: int = 20,
) -> BenchResult:
    """Run the retrieval benchmark and return metrics."""
    store = Store(config.database)
    await store.connect()

    # ── Seed the database with eval memories + distractors ──
    log.info("Embedding eval facts + distractors...")

    all_facts = [pair["fact"] for pair in EVAL_PAIRS]
    all_distractors = DISTRACTORS[:num_distractors]
    all_texts = all_facts + all_distractors

    embeddings = await embed_batch(all_texts)

    # Insert eval facts
    fact_ids: list[int] = []
    for i, pair in enumerate(EVAL_PAIRS):
        mem = Memory(
            content=pair["fact"],
            memory_type=pair["type"],
            confidence=1.0,
            embedding=embeddings[i],
        )
        mem_id = await store.insert_memory(mem)
        fact_ids.append(mem_id)

    # Insert distractors
    for i, text in enumerate(all_distractors):
        mem = Memory(
            content=text,
            memory_type="fact",
            confidence=0.5,
            embedding=embeddings[len(EVAL_PAIRS) + i],
        )
        await store.insert_memory(mem)

    total_memories = len(EVAL_PAIRS) + len(all_distractors)
    log.info(f"Inserted {total_memories} memories ({len(EVAL_PAIRS)} eval + {len(all_distractors)} distractors)")

    # ── Run queries and measure recall ──
    hits_at_1 = 0
    hits_at_5 = 0
    hits_at_10 = 0
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []

    for i, pair in enumerate(EVAL_PAIRS):
        query = pair["query"]
        expected_content = pair["fact"]

        t0 = time.perf_counter()
        query_embedding = await embed(query, is_query=True)
        results = await store.search_memories(query_embedding, limit=10, query_text=query)
        latency_ms = (time.perf_counter() - t0) * 1000
        latencies.append(latency_ms)

        # Find rank of expected result
        rank = None
        for j, r in enumerate(results):
            if r["content"] == expected_content:
                rank = j + 1
                break

        if rank is not None:
            if rank <= 1:
                hits_at_1 += 1
            if rank <= 5:
                hits_at_5 += 1
            if rank <= 10:
                hits_at_10 += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

        status = f"rank={rank}" if rank else "MISS"
        log.info(f"  Q{i+1:02d}: {status:>8s} ({latency_ms:6.1f}ms) | {query[:60]}")

    n = len(EVAL_PAIRS)
    result = BenchResult(
        total_queries=n,
        recall_at_1=hits_at_1 / n,
        recall_at_5=hits_at_5 / n,
        recall_at_10=hits_at_10 / n,
        mrr=sum(reciprocal_ranks) / n,
        avg_latency_ms=sum(latencies) / n,
        memories_in_db=total_memories,
    )

    # ── Cleanup: remove benchmark data ──
    log.info("Cleaning up benchmark data...")
    all_ids = fact_ids
    # Delete all memories we inserted (eval + distractors)
    await store.pool.execute(
        "DELETE FROM memories WHERE id >= $1", min(fact_ids),
    )

    await store.close()
    return result


def print_report(result: BenchResult):
    """Print a formatted benchmark report."""
    print("\n" + "=" * 60)
    print("  Ada Memory Retrieval Benchmark")
    print("  (pgvector + BAAI/bge-small-en-v1.5, 384-dim)")
    print("=" * 60)
    print(f"  Queries:          {result.total_queries}")
    print(f"  Memories in DB:   {result.memories_in_db}")
    print(f"  Avg latency:      {result.avg_latency_ms:.1f}ms")
    print("-" * 60)
    print(f"  R@1:              {result.recall_at_1:.1%}")
    print(f"  R@5:              {result.recall_at_5:.1%}")
    print(f"  R@10:             {result.recall_at_10:.1%}")
    print(f"  MRR:              {result.mrr:.4f}")
    print("=" * 60)

    # Compare to MemPalace reference
    print("\n  Reference: MemPalace (ChromaDB) = 96.6% R@5 on LongMemEval")
    if result.recall_at_5 >= 0.95:
        print("  ✓ Ada meets or exceeds MemPalace baseline")
    elif result.recall_at_5 >= 0.90:
        print("  ~ Ada is competitive with MemPalace")
    else:
        print("  ✗ Below MemPalace baseline — investigate embedding quality")
    print()


def main():
    parser = argparse.ArgumentParser(description="Ada memory retrieval benchmark")
    parser.add_argument(
        "--distractors", type=int, default=20,
        help="Number of distractor memories to inject (default: 20)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = AdaConfig()
    result = asyncio.run(run_benchmark(config, num_distractors=args.distractors))
    print_report(result)


if __name__ == "__main__":
    main()
