"""Noteboard renderer — converts conversation and state into structured display sections.

Sections: LIVE (streaming text), PINNED, TASKS, SCHEDULED
"""

from datetime import datetime
from typing import Any


class NoteboardRenderer:
    """Renders noteboard state into sections for the UI."""

    def render(self, notes: list[dict], tasks: list[dict]) -> dict:
        """Render full noteboard state into UI-ready sections."""
        return {
            "live": self._render_live(notes),
            "pinned": self._render_pinned(notes),
            "tasks": self._render_tasks(tasks),
            "scheduled": self._render_scheduled(notes),
            "rendered_at": datetime.now().isoformat(),
        }

    def _render_live(self, notes: list[dict]) -> list[dict]:
        """Active/captured notes — what's happening now."""
        return [
            self._format_note(n) for n in notes
            if n.get("status") in ("captured", "candidate_task")
            and n.get("visibility") != "personal"
        ]

    def _render_pinned(self, notes: list[dict]) -> list[dict]:
        """Pinned notes — persistent important items."""
        return [self._format_note(n) for n in notes if n.get("status") == "pinned"]

    def _render_tasks(self, tasks: list[dict]) -> list[dict]:
        """Active tasks with status."""
        return [
            {
                "task_id": t["task_id"],
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "owner": t.get("owner", ""),
                "priority": t.get("priority", ""),
                "dispatch_target": t.get("dispatch_target"),
            }
            for t in tasks
            if t.get("status") not in ("done", "failed", "cancelled")
        ]

    def _render_scheduled(self, notes: list[dict]) -> list[dict]:
        """Scheduled notes — time-triggered items."""
        return [self._format_note(n) for n in notes if n.get("type") == "scheduled"]

    def _format_note(self, note: dict) -> dict:
        return {
            "note_id": note.get("note_id", ""),
            "content": note.get("content", ""),
            "type": note.get("type", ""),
            "status": note.get("status", ""),
            "priority": note.get("priority", "low"),
            "visibility": note.get("visibility", "personal"),
            "linked_task_id": note.get("linked_task_id"),
        }
