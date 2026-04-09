"""Data models for the Sentinel error intelligence system."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any


class DiagnosticVerdict(Enum):
    PASS = auto()         # Diagnostic found no issues
    WARN = auto()         # Something looks off but not broken
    FAIL = auto()         # Confirmed problem found
    INCONCLUSIVE = auto() # Couldn't determine (e.g. service down)
    HARNESS_BLOCKED = auto()  # Script violated harness rules


class Severity(Enum):
    LOW = auto()       # Degraded but functional (fallback used)
    MEDIUM = auto()    # Feature broken, system stable
    HIGH = auto()      # Subsystem down (e.g. APU, store, voice)
    CRITICAL = auto()  # System-level failure, session at risk


class PatchVerdict(Enum):
    PENDING = auto()       # Awaiting review
    APPROVED = auto()      # Passed all gates, ready to apply
    REJECTED = auto()      # Failed validation
    APPLIED = auto()       # Successfully applied to system
    ROLLED_BACK = auto()   # Applied but reverted


@dataclass
class ErrorSignature:
    """Unique fingerprint for an error — used for deduplication."""
    exception_type: str
    module: str              # e.g. "ada.apu.orchestrator"
    function: str            # e.g. "ensure_loaded"
    message_hash: str        # SHA-256 of normalized error message (strip dynamic parts)
    line_hint: int | None = None  # approximate line number (may drift between versions)

    @property
    def key(self) -> str:
        return f"{self.module}:{self.function}:{self.exception_type}:{self.message_hash[:12]}"


@dataclass
class ProbeResult:
    """Raw capture from the probe — everything that happened at the crash site."""
    error_signature: ErrorSignature
    timestamp: datetime
    exception: str           # full repr including type
    traceback: str           # formatted traceback string
    locals_snapshot: dict[str, str]   # key → repr(value), truncated
    # System context at time of error
    voice_state: str | None = None
    executive_state: str | None = None
    overlay_state: str | None = None
    # Surrounding context
    recent_decision_event_id: str | None = None
    recent_input: str | None = None
    active_task_ids: list[str] = field(default_factory=list)
    # Reproduction
    reproduction_input: str | None = None      # input that triggered it (if known)
    reproduction_attempted: bool = False
    reproduction_succeeded: bool = False
    # Extra diagnostics
    gpu_vram_snapshot: dict[str, Any] | None = None
    db_pool_stats: dict[str, Any] | None = None


@dataclass
class ErrorReport:
    """The case file — self-contained, everything an LLM needs to write a fix.

    This is the artifact that gets exported, reviewed, and fed to a code-writing LLM.
    """
    report_id: str
    created_at: datetime
    severity: Severity
    probe: ProbeResult
    # Analysis
    title: str               # human-readable one-liner
    subsystem: str           # which Ada subsystem (apu, memory, voice, executive, decision, etc.)
    root_cause_hint: str     # best-guess root cause from pattern matching
    affected_files: list[str]       # files involved in the traceback
    related_error_ids: list[str]    # past errors with similar signature
    occurrence_count: int           # how many times this signature has been seen
    first_seen: datetime
    last_seen: datetime
    # Code context
    source_snippets: dict[str, str]  # file_path → relevant source code around crash site
    # Suggested fix area
    fix_target_files: list[str]      # files the fix should touch
    fix_hint: str                    # brief suggestion for what to change
    # Status
    resolution_patch_id: str | None = None   # links to Patch that resolved this
    resolved: bool = False


@dataclass
class Patch:
    """A proposed fix entering through the Gate."""
    patch_id: str
    created_at: datetime
    target_report_id: str    # which ErrorReport this fixes
    description: str         # what the patch does
    diff: str                # unified diff format
    affected_files: list[str]
    # Authorship
    author: str              # "llm:<model>" or "human:<name>"
    source_prompt: str | None = None   # the prompt that generated this patch (for audit)
    # Validation
    verdict: PatchVerdict = PatchVerdict.PENDING
    syntax_valid: bool = False
    scope_valid: bool = False         # only touches files the error touched
    sandbox_passed: bool = False      # ran in sandbox without new errors
    approval_hash: str | None = None  # user's approval signature
    # Application
    applied_at: datetime | None = None
    rolled_back_at: datetime | None = None
    rollback_reason: str | None = None


@dataclass
class DiagnosticReport:
    """Result of a user-triggered diagnostic run.

    The user asks a question like "is memory consolidation actually working?"
    The coding LLM writes a read-only diagnostic script, the harness validates
    and runs it, and the results are captured here.
    """
    diagnostic_id: str
    created_at: datetime
    user_question: str          # what the user asked to investigate
    target_subsystem: str       # which subsystem is being probed
    # The generated script
    script_source: str          # the Python script the LLM wrote
    harness_verdict: str        # "passed" or reason for rejection
    harness_violations: list[str] = field(default_factory=list)
    # Execution
    executed: bool = False
    execution_stdout: str = ""
    execution_stderr: str = ""
    execution_time_ms: int = 0
    execution_exit_code: int = -1
    # Analysis
    verdict: DiagnosticVerdict = DiagnosticVerdict.INCONCLUSIVE
    findings: list[str] = field(default_factory=list)   # bullet-point findings
    measurements: dict[str, Any] = field(default_factory=dict)  # metric_name → value
    recommendation: str = ""    # what to do about it
    # LLM metadata
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
