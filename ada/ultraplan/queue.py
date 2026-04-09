"""
UltraPlan task queue — PostgreSQL-backed (same database as all AdaOS memory).

Lifecycle: submitted → claimed → planning → completed → reviewed
                                    ↓
                                  failed → retry (up to 2)

Uses the shared Store for PostgreSQL access. Table: ultraplan_queue (defined in schema.sql).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import uuid4

from ..memory.store import Store


class PlanStatus(Enum):
    SUBMITTED = "submitted"      # User or Ada queued it
    CLAIMED = "claimed"          # Daemon picked it up
    PLANNING = "planning"        # Active generation in progress
    COMPLETED = "completed"      # All passes done, plan ready
    REVIEWED = "reviewed"        # User saw it (morning briefing)
    FAILED = "failed"            # Planning failed after retries
    CANCELLED = "cancelled"      # User cancelled before execution


@dataclass
class PlanTask:
    task_id: str                      # e.g. "uplan_20260402_001"
    title: str                        # Short description
    brief: str                        # What to plan — from user or Ada Executive
    domain: str                       # sportwave | blackdirt | adaos | general | legal
    priority: int = 0                 # Higher = planned first tonight
    status: PlanStatus = PlanStatus.SUBMITTED
    linked_task_id: str | None = None  # Link to Ada Executive task if promoted
    submitted_at: datetime | None = None
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    reviewed_at: datetime | None = None
    passes_completed: int = 0
    final_plan: str | None = None     # The synthesized output
    intermediate_outputs: dict | None = None  # {pass_1: ..., pass_2: ..., pass_3: ...}
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 2


class PlanQueue:
    """PostgreSQL-backed planning queue using the shared Store."""

    def __init__(self, store: Store):
        self.store = store

    async def submit(self, title: str, brief: str, domain: str = "general",
                     priority: int = 0, linked_task_id: str | None = None) -> PlanTask:
        """Queue a planning task for overnight processing."""
        task_id = f"uplan_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}"
        now = datetime.now()
        await self.store.pool.execute(
            """INSERT INTO ultraplan_queue (task_id, title, brief, domain, priority, status, linked_task_id, submitted_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            task_id, title, brief, domain, priority, "submitted", linked_task_id, now,
        )
        return PlanTask(
            task_id=task_id, title=title, brief=brief, domain=domain,
            priority=priority, linked_task_id=linked_task_id, submitted_at=now,
        )

    async def get_pending(self, limit: int = 1) -> list[PlanTask]:
        """Get next task(s) to plan, ordered by priority desc, submitted_at asc."""
        rows = await self.store.pool.fetch(
            """SELECT * FROM ultraplan_queue
               WHERE status = 'submitted'
               ORDER BY priority DESC, submitted_at ASC LIMIT $1""",
            limit,
        )
        return [self._row_to_task(r) for r in rows]

    async def claim(self, task_id: str) -> PlanTask:
        """Mark task as claimed by daemon."""
        row = await self.store.pool.fetchrow(
            """UPDATE ultraplan_queue
               SET status = 'claimed', claimed_at = NOW()
               WHERE task_id = $1 RETURNING *""",
            task_id,
        )
        return self._row_to_task(row)

    async def update_progress(self, task_id: str, pass_num: int, output: str) -> None:
        """Store intermediate pass output."""
        await self.store.pool.execute(
            """UPDATE ultraplan_queue
               SET passes_completed = $2,
                   intermediate_outputs = COALESCE(intermediate_outputs, '{}'::jsonb) || $3::jsonb,
                   status = 'planning'
               WHERE task_id = $1""",
            task_id, pass_num, json.dumps({f"pass_{pass_num}": output}),
        )

    async def complete(self, task_id: str, plan: str) -> None:
        """Mark task as completed with final synthesized plan."""
        await self.store.pool.execute(
            """UPDATE ultraplan_queue
               SET status = 'completed', final_plan = $2, completed_at = NOW()
               WHERE task_id = $1""",
            task_id, plan,
        )

    async def mark_reviewed(self, task_id: str) -> None:
        """Mark as reviewed after user sees it in morning briefing."""
        await self.store.pool.execute(
            """UPDATE ultraplan_queue
               SET status = 'reviewed', reviewed_at = NOW()
               WHERE task_id = $1""",
            task_id,
        )

    async def fail(self, task_id: str, error: str) -> None:
        """Mark as failed. Reset to submitted if retries remain."""
        await self.store.pool.execute(
            """UPDATE ultraplan_queue
               SET retry_count = retry_count + 1,
                   error = $2,
                   status = CASE
                       WHEN retry_count + 1 >= max_retries THEN 'failed'
                       ELSE 'submitted'
                   END
               WHERE task_id = $1""",
            task_id, error,
        )

    async def get_completed_unreviewed(self) -> list[PlanTask]:
        """Get plans ready for morning review."""
        rows = await self.store.pool.fetch(
            "SELECT * FROM ultraplan_queue WHERE status = 'completed'"
        )
        return [self._row_to_task(r) for r in rows]

    async def cancel(self, task_id: str) -> None:
        """Cancel a queued task."""
        await self.store.pool.execute(
            """UPDATE ultraplan_queue SET status = 'cancelled'
               WHERE task_id = $1 AND status IN ('submitted', 'claimed')""",
            task_id,
        )

    def _row_to_task(self, row) -> PlanTask:
        return PlanTask(
            task_id=row["task_id"],
            title=row["title"],
            brief=row["brief"],
            domain=row["domain"],
            priority=row["priority"],
            status=PlanStatus(row["status"]),
            linked_task_id=row.get("linked_task_id"),
            submitted_at=row.get("submitted_at"),
            claimed_at=row.get("claimed_at"),
            completed_at=row.get("completed_at"),
            reviewed_at=row.get("reviewed_at"),
            passes_completed=row.get("passes_completed", 0),
            final_plan=row.get("final_plan"),
            intermediate_outputs=row.get("intermediate_outputs"),
            error=row.get("error"),
            retry_count=row.get("retry_count", 0),
            max_retries=row.get("max_retries", 2),
        )
