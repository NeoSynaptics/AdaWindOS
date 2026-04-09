"""Error Registry — persistent error pattern database with deduplication.

Stores error signatures and their reports in PostgreSQL. Tracks frequency,
recurrence, and resolution status. Deduplicates by signature key so the
same bug doesn't generate 500 reports.
"""

import json
import logging
from datetime import datetime
from typing import Any

from .models import ErrorReport, ErrorSignature, ProbeResult, Severity
from .report import ReportBuilder

log = logging.getLogger("ada.sentinel.registry")

# In-memory cache of recent signatures to avoid DB round-trips for hot errors
_DEDUP_WINDOW_SEC = 60  # suppress duplicate reports within this window


class ErrorRegistry:
    """Persistent error pattern database backed by PostgreSQL.

    Responsibilities:
    - Deduplication: same signature within window → bump count, don't create new report
    - History: track first_seen, last_seen, occurrence_count per signature
    - Correlation: find related errors by module, subsystem, or exception type
    - Resolution tracking: link reports to patches that fixed them
    """

    def __init__(self, store, report_builder: ReportBuilder | None = None):
        self.store = store  # ada.memory.store.Store
        self.report_builder = report_builder or ReportBuilder(registry=self)
        self._recent: dict[str, datetime] = {}  # signature_key → last_seen (in-memory dedup)

    async def ingest(self, probe: ProbeResult, subsystem: str) -> ErrorReport | None:
        """Process a captured error. Returns a new report, or None if deduplicated.

        Flow:
        1. Check in-memory dedup cache
        2. Check DB for existing signature
        3. If exists: bump count + update last_seen, return None
        4. If new: build report, persist, return report
        """
        sig_key = probe.error_signature.key
        now = datetime.now()

        # In-memory dedup: suppress rapid-fire duplicates
        if sig_key in self._recent:
            delta = (now - self._recent[sig_key]).total_seconds()
            if delta < _DEDUP_WINDOW_SEC:
                log.debug(f"Sentinel dedup: suppressed {sig_key} ({delta:.0f}s < {_DEDUP_WINDOW_SEC}s)")
                # Still bump the DB count
                await self._bump_occurrence(sig_key)
                return None
        self._recent[sig_key] = now

        # DB lookup: does this signature already have a report?
        existing = await self._get_by_signature(sig_key)

        if existing:
            # Bump occurrence count and update last_seen
            await self._bump_occurrence(sig_key)
            occurrence_count = existing.get("occurrence_count", 1) + 1
            log.info(f"Sentinel: known error {sig_key}, occurrence #{occurrence_count}")

            # If it's been seen many times and is unresolved, elevate severity in logs
            if occurrence_count >= 10 and not existing.get("resolved"):
                log.warning(
                    f"Sentinel: recurring unresolved error {sig_key} "
                    f"({occurrence_count} occurrences since {existing.get('first_seen')})"
                )
            return None

        # New error — build full report
        report = self.report_builder.build(
            probe=probe,
            related_ids=await self._find_related(probe.error_signature),
            occurrence_count=1,
            first_seen=now,
        )

        # Persist to DB
        await self._persist_report(report)

        log.info(f"Sentinel: NEW error registered — {report.report_id}: {report.title}")
        return report

    async def get_report(self, report_id: str) -> dict | None:
        """Fetch a report by ID."""
        try:
            row = await self.store.pool.fetchrow(
                "SELECT * FROM sentinel_reports WHERE report_id = $1", report_id,
            )
            return dict(row) if row else None
        except Exception as e:
            log.error(f"Failed to fetch report {report_id}: {e}")
            return None

    async def get_unresolved(self, limit: int = 50) -> list[dict]:
        """Get all unresolved error reports, ordered by severity + occurrence count."""
        try:
            rows = await self.store.pool.fetch(
                """SELECT * FROM sentinel_reports
                   WHERE resolved = FALSE
                   ORDER BY
                     CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                          WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                     occurrence_count DESC
                   LIMIT $1""",
                limit,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"Failed to fetch unresolved reports: {e}")
            return []

    async def get_by_subsystem(self, subsystem: str, limit: int = 20) -> list[dict]:
        """Get reports for a specific subsystem."""
        try:
            rows = await self.store.pool.fetch(
                """SELECT * FROM sentinel_reports
                   WHERE subsystem = $1
                   ORDER BY last_seen DESC LIMIT $2""",
                subsystem, limit,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"Failed to fetch reports for {subsystem}: {e}")
            return []

    async def mark_resolved(self, report_id: str, patch_id: str) -> None:
        """Mark a report as resolved by a patch."""
        try:
            await self.store.pool.execute(
                """UPDATE sentinel_reports
                   SET resolved = TRUE, resolution_patch_id = $2, updated_at = NOW()
                   WHERE report_id = $1""",
                report_id, patch_id,
            )
            # Also mark all reports with the same signature as resolved
            row = await self.store.pool.fetchrow(
                "SELECT signature_key FROM sentinel_reports WHERE report_id = $1", report_id,
            )
            if row:
                await self.store.pool.execute(
                    """UPDATE sentinel_reports
                       SET resolved = TRUE, resolution_patch_id = $2, updated_at = NOW()
                       WHERE signature_key = $1 AND resolved = FALSE""",
                    row["signature_key"], patch_id,
                )
        except Exception as e:
            log.error(f"Failed to mark report {report_id} as resolved: {e}")

    async def get_stats(self) -> dict[str, Any]:
        """Get summary statistics for the error registry."""
        try:
            row = await self.store.pool.fetchrow("""
                SELECT
                    COUNT(*) AS total_reports,
                    COUNT(*) FILTER (WHERE resolved = FALSE) AS unresolved,
                    COUNT(*) FILTER (WHERE resolved = TRUE) AS resolved,
                    COUNT(DISTINCT signature_key) AS unique_signatures,
                    SUM(occurrence_count) AS total_occurrences,
                    MAX(last_seen) AS most_recent
                FROM sentinel_reports
            """)
            return dict(row) if row else {}
        except Exception as e:
            log.error(f"Failed to get registry stats: {e}")
            return {}

    # --- Internal methods ---

    async def _get_by_signature(self, sig_key: str) -> dict | None:
        try:
            row = await self.store.pool.fetchrow(
                "SELECT * FROM sentinel_reports WHERE signature_key = $1 ORDER BY created_at DESC LIMIT 1",
                sig_key,
            )
            return dict(row) if row else None
        except Exception as e:
            log.error(f"Registry DB lookup failed for {sig_key}: {e}")
            return None

    async def _bump_occurrence(self, sig_key: str) -> None:
        try:
            await self.store.pool.execute(
                """UPDATE sentinel_reports
                   SET occurrence_count = occurrence_count + 1,
                       last_seen = NOW(),
                       updated_at = NOW()
                   WHERE signature_key = $1 AND resolved = FALSE""",
                sig_key,
            )
        except Exception as e:
            log.error(f"Failed to bump occurrence for {sig_key}: {e}")

    async def _find_related(self, sig: ErrorSignature, limit: int = 5) -> list[str]:
        """Find reports with similar signatures (same module or exception type)."""
        try:
            rows = await self.store.pool.fetch(
                """SELECT report_id FROM sentinel_reports
                   WHERE (module = $1 OR exception_type = $2)
                     AND signature_key != $3
                   ORDER BY last_seen DESC LIMIT $4""",
                sig.module, sig.exception_type, sig.key, limit,
            )
            return [r["report_id"] for r in rows]
        except Exception as e:
            log.error(f"Failed to find related errors: {e}")
            return []

    async def _persist_report(self, report: ErrorReport) -> None:
        """Insert a new report into the database."""
        try:
            data = self.report_builder.to_dict(report)
            await self.store.pool.execute(
                """INSERT INTO sentinel_reports
                   (report_id, created_at, severity, title, subsystem,
                    signature_key, exception_type, module, function, message_hash,
                    root_cause_hint, exception, traceback, locals_snapshot,
                    voice_state, executive_state, overlay_state,
                    recent_event_id, recent_input, active_task_ids,
                    affected_files, source_snippets, fix_target_files, fix_hint,
                    related_error_ids, occurrence_count, first_seen, last_seen,
                    resolved, resolution_patch_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30)""",
                data["report_id"],
                report.created_at,
                data["severity"],
                data["title"],
                data["subsystem"],
                data["error_signature_key"],
                data["exception_type"],
                data["module"],
                data["function"],
                data["message_hash"],
                data["root_cause_hint"],
                data["exception"],
                data["traceback"],
                json.dumps(data["locals_snapshot"]),
                data["voice_state"],
                data["executive_state"],
                data["overlay_state"],
                data["recent_event_id"],
                data["recent_input"],
                data["active_task_ids"],
                data["affected_files"],
                json.dumps(data["source_snippets"]),
                data["fix_target_files"],
                data["fix_hint"],
                data["related_error_ids"],
                data["occurrence_count"],
                report.first_seen,
                report.last_seen,
                data["resolved"],
                data["resolution_patch_id"],
            )
        except Exception as e:
            log.error(f"Failed to persist report {report.report_id}: {e}", exc_info=True)
