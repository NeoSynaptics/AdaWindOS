"""Report builder — generates structured, self-contained error case files.

The report is the central artifact: paste it into any LLM and it has everything
needed to understand the bug and write a fix. Reports are stored in the DB
and can be exported as markdown or JSON.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import ErrorReport, ProbeResult, Severity

log = logging.getLogger("ada.sentinel.report")

# How many lines of source to capture around the crash site
_CONTEXT_LINES = 15

# Ada project root (used to resolve file paths in tracebacks)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _extract_files_from_traceback(tb: str) -> list[str]:
    """Extract unique file paths from a traceback string."""
    files = []
    for line in tb.splitlines():
        line = line.strip()
        if line.startswith("File "):
            # File "/path/to/ada/apu/orchestrator.py", line 42, in ensure_loaded
            path = line.split('"')[1] if '"' in line else ""
            if path and "ada/" in path:
                files.append(path)
    # Deduplicate while preserving order (innermost frame last)
    seen = set()
    unique = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def _read_source_snippet(file_path: str, center_line: int | None) -> str | None:
    """Read source code around a crash site."""
    try:
        path = Path(file_path)
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        if not path.exists():
            return None
        lines = path.read_text().splitlines()
        if center_line is None or center_line < 1:
            # Return first 30 lines as fallback
            return "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines[:30]))
        start = max(0, center_line - _CONTEXT_LINES - 1)
        end = min(len(lines), center_line + _CONTEXT_LINES)
        snippet_lines = []
        for i in range(start, end):
            marker = " >> " if i == center_line - 1 else "    "
            snippet_lines.append(f"{i+1:4d}{marker}| {lines[i]}")
        return "\n".join(snippet_lines)
    except Exception as e:
        log.debug(f"Could not read source for {file_path}: {e}")
        return None


def _infer_subsystem(module: str) -> str:
    """Map module path to Ada subsystem name."""
    parts = module.split(".")
    # ada.apu.orchestrator → apu
    # ada.memory.store → memory
    if len(parts) >= 2:
        return parts[1] if parts[0] == "ada" else parts[0]
    return "unknown"


def _infer_fix_hint(probe: ProbeResult) -> str:
    """Generate a brief fix hint based on error patterns."""
    exc = probe.exception.lower()
    sig = probe.error_signature

    if "connection" in exc or "pool" in exc:
        return "Check database connectivity. Verify PostgreSQL is running and pool config is correct."
    if "timeout" in exc:
        return "Operation timed out. Consider increasing timeout or checking if downstream service is responding."
    if "model" in exc and ("not found" in exc or "404" in exc):
        return "Model not available in Ollama. Run `ollama pull <model>` or check model name in config."
    if "json" in exc and ("decode" in exc or "parse" in exc):
        return "LLM returned malformed JSON. Check prompt template and consider adding retry with stricter schema."
    if "vram" in exc.lower() or "memory" in exc.lower() or "cuda" in exc.lower():
        return "GPU memory issue. Check APU orchestrator eviction logic and VRAM thresholds."
    if "permission" in exc or "denied" in exc:
        return "Permission error. Check file/directory permissions and user context."
    if sig.exception_type == "KeyError":
        return f"Missing key in dict/config. Verify data shape at {sig.module}:{sig.function}."
    if sig.exception_type == "AttributeError":
        return f"Object missing expected attribute. Check if initialization completed in {sig.module}."
    if sig.exception_type == "TypeError":
        return f"Type mismatch in {sig.function}(). Check argument types and return values."

    return f"Review {sig.module}:{sig.function} — exception: {sig.exception_type}"


class ReportBuilder:
    """Builds ErrorReports from ProbeResults, enriched with source context and history."""

    def __init__(self, registry=None):
        self._registry = registry  # ErrorRegistry for history lookups

    def build(
        self,
        probe: ProbeResult,
        related_ids: list[str] | None = None,
        occurrence_count: int = 1,
        first_seen: datetime | None = None,
    ) -> ErrorReport:
        """Build a full ErrorReport from a ProbeResult."""
        now = datetime.now()
        report_id = f"rpt_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"

        affected_files = _extract_files_from_traceback(probe.traceback)
        subsystem = _infer_subsystem(probe.error_signature.module)

        # Gather source snippets around crash sites
        source_snippets = {}
        for fpath in affected_files:
            # Try to find the line number for this file in the traceback
            line_num = None
            if fpath in probe.traceback:
                for tb_line in probe.traceback.splitlines():
                    if fpath in tb_line and "line " in tb_line:
                        try:
                            line_num = int(tb_line.split("line ")[1].split(",")[0].strip())
                        except (ValueError, IndexError):
                            pass
                        break
            snippet = _read_source_snippet(fpath, line_num)
            if snippet:
                source_snippets[fpath] = snippet

        # If we have a registry, look up history
        if self._registry and not related_ids:
            related_ids = []  # will be populated by caller from registry

        report = ErrorReport(
            report_id=report_id,
            created_at=now,
            severity=_infer_severity_from_probe(probe, subsystem),
            probe=probe,
            title=f"[{subsystem.upper()}] {probe.error_signature.exception_type} in {probe.error_signature.function}()",
            subsystem=subsystem,
            root_cause_hint=_infer_fix_hint(probe),
            affected_files=affected_files,
            related_error_ids=related_ids or [],
            occurrence_count=occurrence_count,
            first_seen=first_seen or now,
            last_seen=now,
            source_snippets=source_snippets,
            fix_target_files=affected_files[-2:] if affected_files else [],  # innermost frames
            fix_hint=_infer_fix_hint(probe),
        )

        log.info(f"Report built: {report.report_id} — {report.title}")
        return report

    def to_markdown(self, report: ErrorReport) -> str:
        """Export report as markdown — the format you paste into an LLM."""
        lines = [
            f"# Error Report: {report.report_id}",
            f"**Created:** {report.created_at.isoformat()}",
            f"**Severity:** {report.severity.name}",
            f"**Subsystem:** {report.subsystem}",
            f"**Occurrences:** {report.occurrence_count} (first: {report.first_seen.isoformat()}, last: {report.last_seen.isoformat()})",
            "",
            f"## {report.title}",
            "",
            f"### Root Cause Hint",
            report.root_cause_hint,
            "",
            "### Exception",
            f"```",
            report.probe.exception,
            f"```",
            "",
            "### Traceback",
            f"```python",
            report.probe.traceback,
            f"```",
            "",
        ]

        # System state
        lines.extend([
            "### System State at Time of Error",
            f"- **Voice State:** {report.probe.voice_state or 'unknown'}",
            f"- **Executive State:** {report.probe.executive_state or 'unknown'}",
            f"- **Overlay State:** {report.probe.overlay_state or 'unknown'}",
            f"- **Recent Input:** {report.probe.recent_input or 'none'}",
            f"- **Decision Event:** {report.probe.recent_decision_event_id or 'none'}",
            f"- **Active Tasks:** {', '.join(report.probe.active_task_ids) or 'none'}",
            "",
        ])

        # Locals snapshot
        if report.probe.locals_snapshot:
            lines.extend([
                "### Local Variables at Crash Site",
                "```python",
            ])
            for key, val in report.probe.locals_snapshot.items():
                lines.append(f"{key} = {val}")
            lines.extend(["```", ""])

        # Source snippets
        if report.source_snippets:
            lines.append("### Source Code Context")
            for fpath, snippet in report.source_snippets.items():
                lines.extend([
                    f"#### `{fpath}`",
                    "```python",
                    snippet,
                    "```",
                    "",
                ])

        # Fix guidance
        lines.extend([
            "### Suggested Fix",
            f"**Target files:** {', '.join(report.fix_target_files) or 'unknown'}",
            f"**Hint:** {report.fix_hint}",
            "",
            "### Affected Files",
        ])
        for f in report.affected_files:
            lines.append(f"- `{f}`")
        lines.append("")

        if report.related_error_ids:
            lines.extend([
                "### Related Errors",
                *[f"- `{rid}`" for rid in report.related_error_ids],
                "",
            ])

        # Reproduction info
        if report.probe.reproduction_input:
            lines.extend([
                "### Reproduction",
                f"**Input:** `{report.probe.reproduction_input}`",
                f"**Attempted:** {report.probe.reproduction_attempted}",
                f"**Succeeded:** {report.probe.reproduction_succeeded}",
                "",
            ])

        lines.extend([
            "---",
            f"*Report generated by AdaOS Sentinel. Signature: `{report.probe.error_signature.key}`*",
        ])

        return "\n".join(lines)

    def to_dict(self, report: ErrorReport) -> dict[str, Any]:
        """Export report as a JSON-serializable dict for DB storage."""
        return {
            "report_id": report.report_id,
            "created_at": report.created_at.isoformat(),
            "severity": report.severity.name,
            "title": report.title,
            "subsystem": report.subsystem,
            "root_cause_hint": report.root_cause_hint,
            "error_signature_key": report.probe.error_signature.key,
            "exception_type": report.probe.error_signature.exception_type,
            "module": report.probe.error_signature.module,
            "function": report.probe.error_signature.function,
            "message_hash": report.probe.error_signature.message_hash,
            "exception": report.probe.exception,
            "traceback": report.probe.traceback,
            "locals_snapshot": report.probe.locals_snapshot,
            "voice_state": report.probe.voice_state,
            "executive_state": report.probe.executive_state,
            "overlay_state": report.probe.overlay_state,
            "recent_event_id": report.probe.recent_decision_event_id,
            "recent_input": report.probe.recent_input,
            "active_task_ids": report.probe.active_task_ids,
            "affected_files": report.affected_files,
            "source_snippets": report.source_snippets,
            "fix_target_files": report.fix_target_files,
            "fix_hint": report.fix_hint,
            "related_error_ids": report.related_error_ids,
            "occurrence_count": report.occurrence_count,
            "first_seen": report.first_seen.isoformat(),
            "last_seen": report.last_seen.isoformat(),
            "resolved": report.resolved,
            "resolution_patch_id": report.resolution_patch_id,
        }


def _infer_severity_from_probe(probe: ProbeResult, subsystem: str) -> Severity:
    """Map probe data to severity level."""
    exc_type = probe.error_signature.exception_type

    if exc_type in ("SystemExit", "KeyboardInterrupt", "MemoryError"):
        return Severity.CRITICAL
    if subsystem in ("apu", "voice", "store") and "connection" in probe.exception.lower():
        return Severity.HIGH
    if subsystem in ("apu", "voice"):
        return Severity.HIGH
    if subsystem in ("executive", "decision", "agents"):
        return Severity.MEDIUM
    return Severity.LOW
