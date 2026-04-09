"""Sentinel — error intelligence, diagnostics, reporting, and controlled patch gate.

Captures errors across AdaOS, builds structured case files for LLM-driven fixes,
lets users probe the system via LLM-generated read-only diagnostic scripts,
and enforces a narrow input gate for all code changes entering the system.
"""

from .models import (
    ErrorReport, ProbeResult, Patch, PatchVerdict, ErrorSignature,
    DiagnosticReport, DiagnosticVerdict,
)
from .probe import Probe, sentinel_trap
from .report import ReportBuilder
from .registry import ErrorRegistry
from .gate import PatchGate
from .harness import ScriptHarness, HarnessResult
from .diagnostics import DiagnosticEngine

__all__ = [
    "Probe",
    "sentinel_trap",
    "ReportBuilder",
    "ErrorRegistry",
    "PatchGate",
    "ScriptHarness",
    "HarnessResult",
    "DiagnosticEngine",
    "ErrorReport",
    "ProbeResult",
    "Patch",
    "PatchVerdict",
    "ErrorSignature",
    "DiagnosticReport",
    "DiagnosticVerdict",
]
