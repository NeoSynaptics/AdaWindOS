"""
UltraPlan morning review — present overnight plans to user.

Two interfaces:
  1. Voice (via Ada): Ada summarizes plans in morning greeting
  2. Noteboard: Plans appear as reviewable cards with approve/reject/refine
"""

import logging
import re

from .queue import PlanQueue, PlanTask

log = logging.getLogger(__name__)


def _extract_section(text: str, heading: str) -> list[str]:
    """Extract bullet points from a markdown section by heading."""
    pattern = rf"##\s*{re.escape(heading)}\s*\n(.*?)(?=\n##|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return []
    lines = match.group(1).strip().split("\n")
    return [line.strip("- ").strip() for line in lines if line.strip() and line.strip() != "-"]


class PlanReviewer:
    """Morning review interface for completed overnight plans."""

    def __init__(self, queue: PlanQueue):
        self.queue = queue

    async def get_review_summary(self) -> str:
        """Generate spoken summary for Ada's morning greeting.

        Returns a natural language summary like:
        "Morning Daniel. I ran 6 plans overnight. Two for SportWave,
        one for BlackDirt. Four look solid. Two have open questions."
        """
        completed = await self.queue.get_completed_unreviewed()
        if not completed:
            return ""

        # Group by domain
        by_domain: dict[str, list[PlanTask]] = {}
        open_q_count = 0
        for t in completed:
            by_domain.setdefault(t.domain, []).append(t)
            if t.final_plan and "open question" in t.final_plan.lower():
                open_q_count += 1

        # Build domain breakdown
        parts = []
        for domain, tasks in by_domain.items():
            count = len(tasks)
            label = domain.replace("_", " ").title()
            if count == 1:
                parts.append(f"one for {label}")
            else:
                parts.append(f"{count} for {label}")

        total = len(completed)
        solid = total - open_q_count

        summary = f"Morning Daniel. I ran {total} plan{'s' if total != 1 else ''} overnight. "
        summary += ", ".join(parts) + ". "

        if open_q_count:
            summary += (
                f"{solid} look solid. "
                f"{open_q_count} {'has' if open_q_count == 1 else 'have'} "
                f"open questions I need your input on. "
            )
        else:
            summary += "All look solid. "

        summary += "Want me to walk through them?"
        return summary

    async def get_plan_detail(self, task_id: str) -> dict:
        """Get full plan with all passes for detailed review.

        Returns structured dict with final plan, intermediate passes,
        and parsed sections for easy display.
        """
        completed = await self.queue.get_completed_unreviewed()
        task = next((t for t in completed if t.task_id == task_id), None)
        if not task:
            return {"error": f"Plan {task_id} not found or already reviewed"}

        intermediate = task.intermediate_outputs or {}
        final = task.final_plan or ""

        return {
            "task_id": task_id,
            "title": task.title,
            "domain": task.domain,
            "brief": task.brief,
            "final_plan": final,
            "decomposition": intermediate.get("pass_1", ""),
            "critique": intermediate.get("pass_2", ""),
            "open_questions": _extract_section(final, "Open Questions"),
            "key_decisions": _extract_section(final, "Key Decisions Made"),
            "validation_criteria": _extract_section(final, "Validation Criteria"),
        }

    async def approve(self, task_id: str) -> None:
        """Approve a plan — mark reviewed in queue."""
        await self.queue.mark_reviewed(task_id)
        log.info(f"Plan {task_id} approved")

    async def refine(self, task_id: str, feedback: str) -> None:
        """Send a plan back for another overnight pass with user feedback.

        Resets status to 'submitted' with increased priority so it gets
        picked up first next night. Appends user feedback to the brief.
        """
        await self.queue.store.pool.execute(
            """UPDATE ultraplan_queue
               SET status = 'submitted',
                   priority = priority + 1,
                   brief = brief || E'\n\nUSER FEEDBACK:\n' || $2
               WHERE task_id = $1 AND status = 'completed'""",
            task_id, feedback,
        )
        log.info(f"Plan {task_id} sent back for refinement with feedback")

    async def reject(self, task_id: str, reason: str = "") -> None:
        """Reject a plan — mark as cancelled with reason."""
        await self.queue.cancel(task_id)
        if reason:
            await self.queue.store.pool.execute(
                "UPDATE ultraplan_queue SET error = $2 WHERE task_id = $1",
                task_id, f"Rejected: {reason}",
            )
        log.info(f"Plan {task_id} rejected{f': {reason}' if reason else ''}")
