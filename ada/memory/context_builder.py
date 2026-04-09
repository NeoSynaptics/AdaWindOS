"""Context builder — assembles the LLM prompt from all memory types.

Uses a tiered token budget system (inspired by MemPalace's L0-L3 stack)
to prevent unbounded prompt growth:

  L0: Identity   (~800 tok)  — system prompt, SOUL, personality. Always full.
  L1: Hot        (~2000 tok) — recent episodes, active tasks, pending deliveries.
  L2: Memory     (~1500 tok) — pgvector semantic search against current topic.
  L3: Deep       (~700 tok)  — knowledge graph relations, instruction files.

Each tier is filled in order.  Content that would exceed a tier's budget
is truncated with a [truncated — N items omitted] marker so the LLM
knows information was dropped.
"""

import asyncio
import os
from pathlib import Path
from datetime import datetime

from .store import Store
from ..system_prompt import build_system_prompt
from ..state import SystemState
from ..config import ContextBudgetConfig


def _estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """Conservative token estimate: 1 token ≈ 4 chars for English text."""
    return len(text) // chars_per_token


def _truncate_to_budget(
    lines: list[str],
    budget_tokens: int,
    chars_per_token: int = 4,
) -> list[str]:
    """Keep as many lines as fit within the token budget.

    Returns the kept lines.  If any were dropped, appends a marker.
    """
    kept: list[str] = []
    used = 0
    budget_chars = budget_tokens * chars_per_token

    for line in lines:
        line_chars = len(line)
        if used + line_chars > budget_chars and kept:
            remaining = len(lines) - len(kept)
            kept.append(f"[truncated — {remaining} items omitted]")
            break
        kept.append(line)
        used += line_chars

    return kept


class ContextBuilder:
    def __init__(self, store: Store, state: SystemState, embed_fn=None,
                 budget: ContextBudgetConfig | None = None):
        self.store = store
        self.state = state
        self.embed_fn = embed_fn  # async function: str -> list[float]
        self.budget = budget or ContextBudgetConfig()

    async def build(
        self,
        session_id: str,
        user_message: str,
        max_episodes: int = 20,
        max_memories: int = 10,
        max_entities: int = 5,
    ) -> str:
        """Build the full context for the LLM using tiered token budgets."""
        cpt = self.budget.chars_per_token

        # --- Parallel data fetch ---
        embedding_task = (
            self.embed_fn(user_message)
            if self.embed_fn and user_message
            else None
        )

        active_tasks_t, pending_t, budget_t, episodes_t = await asyncio.gather(
            self._format_active_tasks(),
            self._format_pending_deliveries(),
            self.store.get_cloud_tokens_today(),
            self.store.get_recent_episodes(session_id, limit=max_episodes),
        )

        # ── L0: Identity (system prompt — always included, never truncated) ──
        system = build_system_prompt(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            session_id=session_id,
            active_tasks=active_tasks_t,
            pending_deliveries=pending_t,
            voice_state=self.state.voice.name,
            executive_state=self.state.executive.name,
            budget_spent_today=budget_t,
            instruction_files=self._load_instruction_files(),
        )
        parts = [system]

        # ── L1: Hot context (episodes + tasks + deliveries) ──
        l1_lines: list[str] = []

        if episodes_t:
            l1_lines.append("\n# CONVERSATION HISTORY\n")
            for ep in episodes_t:
                l1_lines.append(f"{ep['speaker']}: {ep['content']}")

        # Active tasks go into L1 (hot context, not L3)
        tasks_t = await self.store.get_active_tasks()
        if tasks_t:
            l1_lines.append("\n# ACTIVE TASKS\n")
            for t in tasks_t[:10]:
                l1_lines.append(
                    f"- {t['task_id']}: {t['title']} (status={t['status']}, "
                    f"owner={t['owner']}, priority={t['priority']})"
                )

        l1_trimmed = _truncate_to_budget(l1_lines, self.budget.l1_hot_tokens, cpt)
        parts.extend(l1_trimmed)

        # ── L2: Relevant memories (semantic search) ──
        if embedding_task:
            embedding = await embedding_task

            memories_t, entities_t = await asyncio.gather(
                self.store.search_memories(embedding, limit=max_memories, query_text=user_message),
                self._find_relevant_entities(user_message, limit=max_entities),
            )

            if memories_t:
                l2_lines = ["\n# RELEVANT MEMORIES\n"]
                for mem in memories_t:
                    l2_lines.append(
                        f"[{mem['memory_type']}, confidence={mem['confidence']:.2f}] "
                        f"{mem['content']}"
                    )
                l2_trimmed = _truncate_to_budget(
                    l2_lines, self.budget.l2_memory_tokens, cpt,
                )
                parts.extend(l2_trimmed)
        else:
            entities_t = []

        # ── L3: Deep context (knowledge graph + instruction files) ──
        l3_lines: list[str] = []

        if entities_t:
            l3_lines.append("\n# RELATED KNOWLEDGE\n")
            relation_tasks = [
                self.store.get_entity_relations(ent["id"], depth=2)
                for ent in entities_t
            ]
            all_relations = await asyncio.gather(*relation_tasks)
            for ent, relations in zip(entities_t, all_relations):
                l3_lines.append(f"- {ent['name']} ({ent['entity_type']})")
                for rel in relations[:5]:
                    l3_lines.append(
                        f"  → {rel['predicate']} → {rel['object_name']} "
                        f"({rel['object_type']})"
                    )

        if l3_lines:
            l3_trimmed = _truncate_to_budget(
                l3_lines, self.budget.l3_deep_tokens, cpt,
            )
            parts.extend(l3_trimmed)

        return "\n".join(parts)

    async def build_for_classification(
        self,
        session_id: str,
        user_message: str,
    ) -> dict:
        """Build lightweight context for intent classification (not full prompt)."""
        episodes = await self.store.get_recent_episodes(session_id, limit=5)
        last_topics = []
        last_intent = "none"
        for ep in episodes:
            if ep["turn_type"] == "user":
                last_topics.append(ep["content"][:100])

        tasks = await self.store.get_active_tasks()
        pending = [f"{t['task_id']}:{t['title'][:50]}({t['status']})" for t in tasks[:5]]

        return {
            "active_topic": last_topics[0] if last_topics else "none",
            "pending_tasks": ", ".join(pending) if pending else "none",
            "last_intent": last_intent,
        }

    async def _format_active_tasks(self) -> str:
        tasks = await self.store.get_active_tasks()
        if not tasks:
            return "none"
        lines = []
        for t in tasks[:5]:
            lines.append(f"{t['task_id']}:{t['title']}({t['status']})")
        return ", ".join(lines)

    async def _format_pending_deliveries(self) -> str:
        outbox = await self.store.fetch_pending_outbox(batch=5)
        if not outbox:
            return "none"
        return f"{len(outbox)} pending"

    async def _find_relevant_entities(self, text: str, limit: int = 5) -> list[dict]:
        """Find entities relevant to the current message via embedding similarity."""
        if not self.embed_fn:
            return []
        embedding = await self.embed_fn(text)
        vec_str = "[" + ",".join(str(f) for f in embedding) + "]"
        rows = await self.store.pool.fetch(
            """SELECT *, embedding <=> $1::vector AS distance
               FROM entities WHERE embedding IS NOT NULL
               ORDER BY distance ASC LIMIT $2""",
            vec_str, limit,
        )
        return [dict(r) for r in rows]

    def _load_instruction_files(self) -> str:
        """Walk directory tree loading .ada/instructions.md files."""
        parts = []
        paths = [
            Path.home() / ".ada" / "instructions.md",
            Path(".ada") / "instructions.md",
        ]

        for path in paths:
            if path.exists():
                content = path.read_text().strip()
                if len(content) > 6000:  # ~1.5K tokens max
                    content = content[:6000] + "\n[truncated]"
                parts.append(f"# Instructions from {path}\n{content}")

        return "\n\n".join(parts) if parts else ""
