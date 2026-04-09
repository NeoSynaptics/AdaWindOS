"""Task lifecycle management — create, assign, validate, retry, deliver.

All methods log errors with task_id context and raise TaskError
so callers can degrade gracefully.
"""

import json
import logging
from datetime import datetime
from uuid import uuid4

log = logging.getLogger(__name__)


class TaskError(Exception):
    """Raised when a task operation fails."""
    pass

from ..memory.store import Store
from ..memory.models import Task, Note, OutboxEvent
from ..journal.logger import Journal
from ..decision.router import route_to_agent


class TaskManager:
    def __init__(self, store: Store, journal: Journal):
        self.store = store
        self.journal = journal

    async def create_task(
        self,
        title: str,
        task_type: str,
        origin: str,
        brief: str,
        priority: str = "medium",
        success_criteria: list[str] | None = None,
        constraints: list[str] | None = None,
        dispatch_target: str | None = None,
        decision_event_id: str | None = None,
        repo_path: str | None = None,
    ) -> Task:
        task = Task(
            task_id=f"tsk_{datetime.now().strftime('%Y%m%d')}_{uuid4().hex[:6]}",
            title=title,
            type=task_type,
            origin=origin,
            brief=brief,
            priority=priority,
            success_criteria=success_criteria or [],
            constraints=constraints or [],
            created_by="ADA",
            dispatch_target=dispatch_target,
        )
        try:
            await self.store.insert_task(task)
        except Exception as e:
            log.error(f"Failed to create task '{title}': {e}", exc_info=True)
            raise TaskError(f"Task creation failed: {e}") from e

        await self.journal.log(
            action_type="task_create",
            summary=f"Created task: {title}",
            task_id=task.task_id,
            band=2,
        )
        return task

    async def dispatch_task(self, task_id: str, decision_event_id: str) -> None:
        """Dispatch a task to a worker via outbox (restart-safe)."""
        task_data = await self.store.get_task(task_id)
        if not task_data:
            log.error(f"Cannot dispatch — task {task_id} not found in database")
            raise TaskError(f"Task {task_id} not found")

        target = task_data.get("dispatch_target") or route_to_agent(task_data.get("brief", ""))

        # Build task packet for the worker
        task_packet = {
            "task_id": task_id,
            "objective": task_data["title"],
            "context": task_data.get("brief", ""),
            "constraints": json.loads(task_data.get("constraints", "[]")),
            "success_criteria": json.loads(task_data.get("success_criteria", "[]")),
            "agent": target,
            # FORGE needs a real repo path — stored on the task or defaults to project root
            "repo_path": task_data.get("repo_path", ""),
        }

        # Write status + outbox in one atomic transaction (architecture requirement)
        try:
            async with self.store.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE tasks SET status = $2, dispatch_target = $3, updated_at = NOW() WHERE task_id = $1",
                        task_id, "dispatched", target,
                    )
                    await conn.execute(
                        """INSERT INTO outbox_events (decision_event_id, event_type, payload, status, visible_at)
                           VALUES ($1, $2, $3, $4, NOW())""",
                        decision_event_id, "agent.dispatch", json.dumps(task_packet), "pending",
                    )
        except Exception as e:
            log.error(f"Atomic dispatch transaction failed for {task_id}: {e}", exc_info=True)
            raise TaskError(f"Dispatch transaction failed for {task_id}: {e}") from e

        await self.journal.log(
            action_type="dispatch",
            summary=f"Dispatched {task_id} to {target}",
            task_id=task_id,
            agent=target,
            band=2,
        )

    async def handle_result(self, task_id: str, artifacts: list[dict]) -> None:
        """Called when a worker returns results. Moves to validating."""
        await self.store.update_task_status(
            task_id, "validating",
            artifacts_received=json.dumps(artifacts),
        )
        await self.journal.log(
            action_type="result_received",
            summary=f"Result received for {task_id}",
            task_id=task_id,
            band=2,
        )

    async def complete_task(self, task_id: str) -> None:
        await self.store.update_task_status(task_id, "done", completed_at=datetime.now())
        await self.journal.log(
            action_type="task_complete",
            summary=f"Task {task_id} completed",
            task_id=task_id,
            band=1,
        )

    async def fail_task(self, task_id: str, reason: str) -> None:
        await self.store.update_task_status(task_id, "failed", escalation_reason=reason)
        await self.journal.log(
            action_type="task_failed",
            summary=f"Task {task_id} failed: {reason}",
            task_id=task_id,
            band=2,
        )

    async def retry_task(self, task_id: str, feedback: str, decision_event_id: str | None) -> bool:
        """Retry a failed task. Returns False if max retries reached."""
        task_data = await self.store.get_task(task_id)
        if not task_data:
            return False

        retry_count = task_data.get("retry_count", 0)
        max_retries = task_data.get("max_retries", 3)

        if retry_count >= max_retries:
            await self.fail_task(task_id, f"Max retries ({max_retries}) exhausted")
            return False

        await self.store.update_task_status(
            task_id, "queued",
            retry_count=retry_count + 1,
            brief=f"{task_data.get('brief', '')}\n\nRETRY FEEDBACK: {feedback}",
        )
        await self.dispatch_task(task_id, decision_event_id)

        await self.journal.log(
            action_type="retry",
            summary=f"Retry {retry_count + 1}/{max_retries} for {task_id}",
            task_id=task_id,
            band=2,
            details={"feedback": feedback, "attempt": retry_count + 1},
        )
        return True

    async def promote_note_to_task(self, note_id: str, title: str, brief: str) -> Task:
        """Promote a note to a task."""
        await self.store.update_note_status(note_id, "promoted_to_task")
        task = await self.create_task(
            title=title,
            task_type="note_followup",
            origin="noteboard",
            brief=brief,
        )
        await self.store.update_note_status(note_id, "promoted_to_task", linked_task_id=task.task_id)
        return task
