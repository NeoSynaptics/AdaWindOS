"""Append-only execution journal. Every action Ada takes is logged here."""

from datetime import datetime
from typing import Any

from ..memory.models import JournalEntry
from ..memory.store import Store


class Journal:
    def __init__(self, store: Store):
        self.store = store

    async def log(
        self,
        action_type: str,
        summary: str,
        task_id: str | None = None,
        agent: str | None = None,
        band: int = 1,
        budget_impact: dict[str, Any] | None = None,
        rollback_hint: str | None = None,
        state_before: str | None = None,
        state_after: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        entry = JournalEntry(
            action_type=action_type,
            action_summary=summary,
            task_id=task_id,
            agent=agent,
            band=band,
            budget_impact=budget_impact or {},
            rollback_hint=rollback_hint,
            state_before=state_before,
            state_after=state_after,
            details=details or {},
            timestamp=datetime.now(),
        )
        await self.store.insert_journal(entry)

    async def cloud_tokens_today(self) -> int:
        return await self.store.get_cloud_tokens_today()

    async def today(self) -> list[dict]:
        return await self.store.get_journal_today()
