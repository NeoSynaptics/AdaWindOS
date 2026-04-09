"""Probe — automatic error capture with full context snapshots.

Wraps async callables. On failure: captures exception, locals, state machine
positions, recent decision events, and system resource state. Optionally
attempts synthetic reproduction in a sandbox.

Usage:
    # As a decorator
    @sentinel_trap(subsystem="apu")
    async def ensure_loaded(self, model: str, gpu: GPU) -> LoadResult:
        ...

    # As a context manager
    async with probe.capture(subsystem="memory", context={"query": sql}):
        result = await pool.fetch(sql)
"""

import asyncio
import hashlib
import inspect
import logging
import traceback
from datetime import datetime
from functools import wraps
from typing import Any, Callable

from .models import ErrorSignature, ProbeResult, Severity

log = logging.getLogger("ada.sentinel.probe")

# Truncation limits to prevent memory bloat in snapshots
_MAX_LOCAL_REPR = 500       # max chars per local variable repr
_MAX_LOCALS = 30            # max number of locals to capture
_MAX_TRACEBACK_LINES = 100  # max traceback lines


def _normalize_message(msg: str) -> str:
    """Strip dynamic content (IDs, timestamps, memory addresses) for stable hashing."""
    import re
    # Strip hex addresses like 0x7f...
    msg = re.sub(r"0x[0-9a-fA-F]+", "<addr>", msg)
    # Strip UUIDs
    msg = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", msg)
    # Strip timestamps
    msg = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "<ts>", msg)
    # Strip numeric IDs that look like sequence numbers
    msg = re.sub(r"(?<=_)\d{5,}(?=\b)", "<seq>", msg)
    return msg


def _hash_message(msg: str) -> str:
    normalized = _normalize_message(msg)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _safe_repr(obj: Any) -> str:
    """Get repr of an object, truncated and safe from exceptions."""
    try:
        r = repr(obj)
        if len(r) > _MAX_LOCAL_REPR:
            return r[:_MAX_LOCAL_REPR] + "…"
        return r
    except Exception:
        return f"<repr failed: {type(obj).__name__}>"


def _capture_locals(frame) -> dict[str, str]:
    """Capture local variables from a frame, truncated and safe."""
    if frame is None:
        return {}
    locals_dict = {}
    for key, val in list(frame.f_locals.items())[:_MAX_LOCALS]:
        if key.startswith("__"):
            continue
        locals_dict[key] = _safe_repr(val)
    return locals_dict


def _extract_crash_frame(tb_str: str) -> tuple[str, str, int | None]:
    """Extract module and function name from the innermost traceback frame."""
    lines = tb_str.strip().splitlines()
    # Walk backwards to find the last 'File "..." line N, in func'
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("File "):
            parts = line.split(", ")
            func = parts[-1].replace("in ", "") if len(parts) >= 3 else "unknown"
            # Extract module from file path
            file_part = parts[0] if parts else ""
            module = file_part.replace('File "', '').rstrip('"')
            # Convert file path to module-style: ada/apu/orchestrator.py → ada.apu.orchestrator
            if "ada/" in module:
                module = module[module.index("ada/"):]
                module = module.replace("/", ".").replace(".py", "")
            line_num = None
            if len(parts) >= 2:
                try:
                    line_num = int(parts[1].replace("line ", "").strip())
                except ValueError:
                    pass
            return module, func, line_num
    return "unknown", "unknown", None


def _infer_severity(exc: BaseException, subsystem: str) -> Severity:
    """Infer error severity from exception type and subsystem."""
    exc_name = type(exc).__name__

    # Critical: anything that kills the session
    if isinstance(exc, (SystemExit, KeyboardInterrupt, MemoryError)):
        return Severity.CRITICAL

    # High: subsystem-level failures
    if subsystem in ("apu", "voice", "store"):
        if "connection" in str(exc).lower() or "pool" in str(exc).lower():
            return Severity.HIGH
        return Severity.HIGH

    # Medium: feature-level failures
    if subsystem in ("executive", "decision", "agents"):
        return Severity.MEDIUM

    # Low: everything else (usually graceful degradation already handled)
    return Severity.LOW


class Probe:
    """Central probe instance — captures errors and builds ProbeResults.

    Holds a reference to the Ada system state for context enrichment.
    """

    def __init__(self):
        self._state = None            # SystemState reference, set by Ada.start()
        self._recent_event_id = None  # updated by process_input()
        self._recent_input = None
        self._active_task_ids: list[str] = []
        self._on_capture: Callable | None = None  # callback when error captured

    def bind(
        self,
        state=None,
        on_capture: Callable | None = None,
    ) -> None:
        """Bind to Ada's runtime state for richer context."""
        if state is not None:
            self._state = state
        if on_capture is not None:
            self._on_capture = on_capture

    def update_context(
        self,
        event_id: str | None = None,
        user_input: str | None = None,
        active_task_ids: list[str] | None = None,
    ) -> None:
        """Called by process_input() to keep probe context fresh."""
        if event_id is not None:
            self._recent_event_id = event_id
        if user_input is not None:
            self._recent_input = user_input
        if active_task_ids is not None:
            self._active_task_ids = active_task_ids

    def capture(self, subsystem: str, context: dict[str, Any] | None = None):
        """Context manager for capturing errors in a code block."""
        return _ProbeContext(self, subsystem, context or {})

    def capture_exception(
        self,
        exc: BaseException,
        subsystem: str,
        extra_locals: dict[str, str] | None = None,
        reproduction_input: str | None = None,
    ) -> ProbeResult:
        """Directly capture an exception into a ProbeResult."""
        tb_str = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_text = "".join(tb_str)
        if len(tb_text.splitlines()) > _MAX_TRACEBACK_LINES:
            lines = tb_text.splitlines()
            tb_text = "\n".join(lines[:10] + ["... truncated ..."] + lines[-_MAX_TRACEBACK_LINES:])

        module, func, line_hint = _extract_crash_frame(tb_text)

        # Capture locals from the innermost frame
        locals_snapshot = extra_locals or {}
        if exc.__traceback__:
            tb = exc.__traceback__
            while tb.tb_next:
                tb = tb.tb_next
            locals_snapshot.update(_capture_locals(tb.tb_frame))

        signature = ErrorSignature(
            exception_type=type(exc).__name__,
            module=module,
            function=func,
            message_hash=_hash_message(str(exc)),
            line_hint=line_hint,
        )

        result = ProbeResult(
            error_signature=signature,
            timestamp=datetime.now(),
            exception=f"{type(exc).__name__}: {exc}",
            traceback=tb_text,
            locals_snapshot=locals_snapshot,
            voice_state=self._state.voice.name if self._state else None,
            executive_state=self._state.executive.name if self._state else None,
            overlay_state=self._state.overlay.name if self._state else None,
            recent_decision_event_id=self._recent_event_id,
            recent_input=self._recent_input,
            active_task_ids=list(self._active_task_ids),
            reproduction_input=reproduction_input,
        )

        log.warning(
            f"Sentinel captured [{signature.key}] in {subsystem}: {type(exc).__name__}: {exc}"
        )

        # Fire callback (non-blocking)
        if self._on_capture:
            try:
                cb_result = self._on_capture(result, subsystem)
                if asyncio.iscoroutine(cb_result):
                    asyncio.ensure_future(cb_result)
            except Exception as cb_err:
                log.error(f"Sentinel on_capture callback failed: {cb_err}")

        return result


class _ProbeContext:
    """Async context manager for probe.capture()."""

    def __init__(self, probe: Probe, subsystem: str, context: dict[str, Any]):
        self.probe = probe
        self.subsystem = subsystem
        self.context = context
        self.result: ProbeResult | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            extra = {k: _safe_repr(v) for k, v in self.context.items()}
            self.result = self.probe.capture_exception(
                exc_val, self.subsystem, extra_locals=extra,
            )
        # Don't suppress the exception — let it propagate
        return False


def sentinel_trap(subsystem: str, probe_instance: Probe | None = None):
    """Decorator that wraps an async function with Sentinel error capture.

    The decorated function still raises — Sentinel only observes.

    Usage:
        @sentinel_trap("apu")
        async def risky_operation(self, ...):
            ...
    """
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            probe = probe_instance or _global_probe
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                # Build extra context from function args
                sig = inspect.signature(fn)
                bound = sig.bind_partial(*args, **kwargs)
                extra = {k: _safe_repr(v) for k, v in bound.arguments.items() if k != "self"}
                probe.capture_exception(exc, subsystem, extra_locals=extra)
                raise  # always re-raise
        return wrapper
    return decorator


# Global probe instance — modules can import and use without wiring
_global_probe = Probe()


def get_probe() -> Probe:
    """Get the global probe instance."""
    return _global_probe
