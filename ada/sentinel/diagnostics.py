"""Diagnostic Engine — user-triggered system probing via LLM-generated scripts.

The user says: "I don't feel like memory consolidation is actually working"
The engine:
1. Gathers relevant codebase context for the subsystem in question
2. Prompts the local coding LLM with the question + codebase + strict rules
3. LLM generates a read-only diagnostic Python script
4. ScriptHarness validates it (AST-level, no side effects allowed)
5. Script runs in a sandboxed subprocess (timeout, read-only, captured stdout)
6. LLM analyzes the raw output and produces a structured diagnostic report
7. Report tells the user exactly what's happening and what to do

The key insight: the LLM has full codebase access so it can write tests
for things the developer didn't think to test. But the harness ensures
it can ONLY read and report — never modify.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .harness import ScriptHarness, HarnessResult
from .models import DiagnosticReport, DiagnosticVerdict

log = logging.getLogger("ada.sentinel.diagnostics")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Max lines of codebase context to feed to the LLM per file
_MAX_CONTEXT_LINES_PER_FILE = 300
# Max total context chars
_MAX_TOTAL_CONTEXT = 80_000
# Script execution timeout
_SCRIPT_TIMEOUT_SEC = 30
# Max stdout/stderr capture
_MAX_OUTPUT_CHARS = 50_000

# Subsystem → relevant source files mapping
_SUBSYSTEM_FILES: dict[str, list[str]] = {
    "memory": [
        "ada/memory/store.py",
        "ada/memory/models.py",
        "ada/memory/context_builder.py",
        "ada/memory/consolidation.py",
        "ada/memory/embeddings.py",
        "ada/memory/schema.sql",
    ],
    "apu": [
        "ada/apu/orchestrator.py",
        "ada/apu/gateway.py",
        "ada/apu/registry.py",
        "ada/apu/monitor.py",
        "ada/apu/models.py",
    ],
    "voice": [
        "ada/voice/pipeline.py",
        "ada/voice/wakeword.py",
    ],
    "decision": [
        "ada/decision/classifier.py",
        "ada/decision/resolver.py",
        "ada/decision/event.py",
        "ada/decision/router.py",
    ],
    "executive": [
        "ada/executive/task_manager.py",
        "ada/executive/validator.py",
        "ada/executive/outbox.py",
        "ada/executive/critic.py",
    ],
    "agents": [
        "ada/agents/dispatcher.py",
    ],
    "ultraplan": [
        "ada/ultraplan/daemon.py",
        "ada/ultraplan/planner.py",
        "ada/ultraplan/queue.py",
    ],
    "sentinel": [
        "ada/sentinel/probe.py",
        "ada/sentinel/registry.py",
        "ada/sentinel/gate.py",
        "ada/sentinel/diagnostics.py",
    ],
    "core": [
        "ada/main.py",
        "ada/config.py",
        "ada/state.py",
    ],
}


# The system prompt that constrains the coding LLM
_DIAGNOSTIC_SYSTEM_PROMPT = """\
You are a diagnostic script writer for AdaOS, a voice-first AI operating system.

The user has a concern about how the system is working. Your job is to write
a focused Python diagnostic script that investigates the concern and reports findings.

## ABSOLUTE RULES — VIOLATION = SCRIPT REJECTED

1. READ ONLY. Never write files, modify databases, or change system state.
2. OUTPUT via print() ONLY. Your script's stdout IS the diagnostic report.
3. No subprocess, os.system, eval, exec, or any command execution.
4. No network mutations (no POST/PUT/DELETE). GET requests for health checks are OK.
5. Database access is SELECT ONLY. No INSERT, UPDATE, DELETE, DROP, ALTER.
6. No imports of: shutil, ctypes, signal, multiprocessing, subprocess, socket, pickle.
7. Keep it focused — investigate the specific concern, nothing else.
8. Script must complete within 30 seconds.

## WHAT YOU CAN DO

- Read any file in the AdaOS codebase (open('path', 'r'))
- Query the PostgreSQL database with SELECT statements via asyncpg
- Check if services are running (HTTP GET to localhost endpoints)
- Inspect Python objects, data structures, configurations
- Measure timing, count records, check consistency
- Read environment variables
- Use standard library: json, datetime, re, collections, statistics, pathlib, os.path

## OUTPUT FORMAT

Print your findings as structured text:
```
=== DIAGNOSTIC: [title] ===
SUBSYSTEM: [name]
TIMESTAMP: [ISO timestamp]

--- CHECKS ---
[✓] Check description... result
[✗] Check description... problem found
[!] Check description... warning

--- MEASUREMENTS ---
metric_name: value
metric_name: value

--- FINDINGS ---
1. Finding description
2. Finding description

--- VERDICT ---
PASS | WARN | FAIL | INCONCLUSIVE

--- RECOMMENDATION ---
What to do about it.
```

## DATABASE ACCESS

The AdaOS PostgreSQL database is at:
- Host: localhost, Port: 5432, Database: ada, User: ada, Password: ada_local

To query it, use this pattern:
```python
import asyncio
import asyncpg

async def query():
    conn = await asyncpg.connect(
        host='localhost', port=5432,
        database='ada', user='ada', password='ada_local'
    )
    try:
        rows = await conn.fetch("SELECT ...")
        # process rows
    finally:
        await conn.close()

asyncio.run(query())
```

## CODEBASE CONTEXT

The following source files are relevant to the investigation:

{codebase_context}

## THE USER'S CONCERN

{user_question}

Write the diagnostic script now. Output ONLY the Python code, no explanation."""


_ANALYSIS_SYSTEM_PROMPT = """\
You are analyzing the output of a diagnostic script run against AdaOS.

The user's original concern was: {user_question}

The diagnostic script produced this output:

```
{script_output}
```

{script_errors}

Analyze the output and provide a structured assessment:

1. VERDICT: One of PASS (no issues found), WARN (something looks off), FAIL (confirmed problem), INCONCLUSIVE (couldn't determine)
2. FINDINGS: Bullet list of specific observations (max 10)
3. MEASUREMENTS: Key metrics as name:value pairs (if any numeric data in output)
4. RECOMMENDATION: Brief, actionable advice (1-3 sentences)

Respond in JSON format:
{{
    "verdict": "PASS|WARN|FAIL|INCONCLUSIVE",
    "findings": ["finding 1", "finding 2"],
    "measurements": {{"metric": "value"}},
    "recommendation": "what to do"
}}"""


class DiagnosticEngine:
    """Runs user-triggered diagnostic investigations.

    The user asks a question → coding LLM writes a diagnostic script →
    harness validates → sandbox executes → analysis LLM interprets results.
    """

    def __init__(
        self,
        gateway=None,           # APUGateway for LLM calls
        coding_model: str = "",  # model name for script generation
        control_model: str = "", # model name for analysis
        store=None,             # Store for persistence
    ):
        self.gateway = gateway
        self.coding_model = coding_model
        self.control_model = control_model
        self.store = store
        self.harness = ScriptHarness()

    async def investigate(self, user_question: str) -> DiagnosticReport:
        """Full diagnostic flow: question → script → validate → run → analyze.

        This is the main entry point. The user says something like:
        "I don't feel like memory consolidation is actually extracting facts"
        and this method does everything.
        """
        now = datetime.now()
        diag_id = f"diag_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        subsystem = self._infer_subsystem(user_question)

        log.info(f"Diagnostic {diag_id}: investigating '{user_question}' in {subsystem}")

        # Step 1: Gather codebase context
        context = self._gather_context(subsystem)

        # Step 2: Ask coding LLM to write the diagnostic script
        script_source = await self._generate_script(user_question, context)

        if not script_source:
            return DiagnosticReport(
                diagnostic_id=diag_id,
                created_at=now,
                user_question=user_question,
                target_subsystem=subsystem,
                script_source="",
                harness_verdict="LLM failed to generate script",
                verdict=DiagnosticVerdict.INCONCLUSIVE,
                findings=["The coding model failed to generate a diagnostic script"],
                recommendation="Try rephrasing your concern or check if the coding model is available.",
                model_used=self.coding_model,
            )

        # Step 3: Validate through harness
        harness_result = self.harness.validate(script_source)

        if not harness_result.passed:
            log.warning(f"Diagnostic {diag_id}: harness BLOCKED — {harness_result.summary}")

            # Give the LLM ONE retry with the violations as feedback
            script_source = await self._retry_script(
                user_question, context, script_source, harness_result,
            )
            if script_source:
                harness_result = self.harness.validate(script_source)

            if not harness_result.passed:
                return DiagnosticReport(
                    diagnostic_id=diag_id,
                    created_at=now,
                    user_question=user_question,
                    target_subsystem=subsystem,
                    script_source=script_source or "",
                    harness_verdict=harness_result.summary,
                    harness_violations=harness_result.violations,
                    verdict=DiagnosticVerdict.HARNESS_BLOCKED,
                    findings=[f"Script violated safety rules: {v}" for v in harness_result.violations],
                    recommendation="The generated script was unsafe. Try a more specific question.",
                    model_used=self.coding_model,
                )

        # Step 4: Execute in sandbox
        stdout, stderr, exit_code, exec_time_ms = await self._execute_sandboxed(script_source)

        # Step 5: Analyze results with control LLM
        analysis = await self._analyze_output(user_question, stdout, stderr)

        # Build report
        report = DiagnosticReport(
            diagnostic_id=diag_id,
            created_at=now,
            user_question=user_question,
            target_subsystem=subsystem,
            script_source=script_source,
            harness_verdict="passed",
            harness_violations=[],
            executed=True,
            execution_stdout=stdout[:_MAX_OUTPUT_CHARS],
            execution_stderr=stderr[:_MAX_OUTPUT_CHARS],
            execution_time_ms=exec_time_ms,
            execution_exit_code=exit_code,
            verdict=self._map_verdict(analysis.get("verdict", "INCONCLUSIVE")),
            findings=analysis.get("findings", []),
            measurements=analysis.get("measurements", {}),
            recommendation=analysis.get("recommendation", ""),
            model_used=self.coding_model,
        )

        # Persist
        if self.store:
            await self._persist_report(report)

        log.info(
            f"Diagnostic {diag_id}: {report.verdict.name} — "
            f"{len(report.findings)} findings, {exec_time_ms}ms"
        )
        return report

    # --- Internal methods ---

    def _infer_subsystem(self, question: str) -> str:
        """Guess which subsystem the user is asking about."""
        q = question.lower()
        keywords = {
            "memory": ["memory", "consolidat", "episode", "semantic", "embed", "remember", "forget"],
            "apu": ["apu", "gpu", "vram", "model load", "ollama", "evict", "swap"],
            "voice": ["voice", "speech", "tts", "stt", "whisper", "listen", "speak", "wake word"],
            "decision": ["classif", "intent", "decision", "route", "resolver"],
            "executive": ["task", "dispatch", "validate", "outbox", "retry", "queue"],
            "agents": ["agent", "arch", "forge", "worker", "openhands"],
            "ultraplan": ["ultraplan", "overnight", "plan", "deep think"],
            "sentinel": ["sentinel", "error", "probe", "diagnostic", "patch"],
            "core": ["config", "state", "startup", "shutdown", "session"],
        }
        best = "core"
        best_score = 0
        for subsystem, kws in keywords.items():
            score = sum(1 for kw in kws if kw in q)
            if score > best_score:
                best_score = score
                best = subsystem
        return best

    def _gather_context(self, subsystem: str) -> str:
        """Read relevant source files and format as context for the LLM."""
        files = _SUBSYSTEM_FILES.get(subsystem, [])
        # Always include core files
        if subsystem != "core":
            files = _SUBSYSTEM_FILES.get("core", []) + files

        context_parts = []
        total_chars = 0

        for rel_path in files:
            full_path = _PROJECT_ROOT / rel_path
            if not full_path.exists():
                continue
            try:
                content = full_path.read_text()
                lines = content.splitlines()
                if len(lines) > _MAX_CONTEXT_LINES_PER_FILE:
                    content = "\n".join(lines[:_MAX_CONTEXT_LINES_PER_FILE])
                    content += f"\n... ({len(lines) - _MAX_CONTEXT_LINES_PER_FILE} more lines)"

                if total_chars + len(content) > _MAX_TOTAL_CONTEXT:
                    break

                context_parts.append(f"### {rel_path}\n```python\n{content}\n```\n")
                total_chars += len(content)
            except Exception as e:
                log.debug(f"Could not read {rel_path}: {e}")

        return "\n".join(context_parts)

    async def _generate_script(self, question: str, context: str) -> str | None:
        """Ask the coding LLM to write a diagnostic script."""
        if not self.gateway:
            log.error("DiagnosticEngine: no gateway configured")
            return None

        prompt = _DIAGNOSTIC_SYSTEM_PROMPT.format(
            codebase_context=context,
            user_question=question,
        )

        try:
            response = await self.gateway.chat_response(
                model=self.coding_model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Write a diagnostic script to investigate: {question}"},
                ],
                temperature=0.3,  # low temp for precise code generation
                timeout=60.0,
            )
            return self._extract_code(response)
        except Exception as e:
            log.error(f"DiagnosticEngine: script generation failed: {e}")
            return None

    async def _retry_script(
        self,
        question: str,
        context: str,
        failed_script: str,
        harness_result: HarnessResult,
    ) -> str | None:
        """Give the LLM one retry with violation feedback."""
        if not self.gateway:
            return None

        violations_text = "\n".join(f"- {v}" for v in harness_result.violations)

        prompt = _DIAGNOSTIC_SYSTEM_PROMPT.format(
            codebase_context=context,
            user_question=question,
        )

        try:
            response = await self.gateway.chat_response(
                model=self.coding_model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Write a diagnostic script to investigate: {question}"},
                    {"role": "assistant", "content": f"```python\n{failed_script}\n```"},
                    {"role": "user", "content": (
                        "That script was REJECTED by the safety harness. Violations:\n"
                        f"{violations_text}\n\n"
                        "Rewrite the script to be strictly read-only. "
                        "Use only SELECT queries, open() in read mode, and print() for output."
                    )},
                ],
                temperature=0.2,
                timeout=60.0,
            )
            return self._extract_code(response)
        except Exception as e:
            log.error(f"DiagnosticEngine: retry generation failed: {e}")
            return None

    async def _execute_sandboxed(
        self, script: str,
    ) -> tuple[str, str, int, int]:
        """Execute a validated script in a sandboxed subprocess.

        Returns (stdout, stderr, exit_code, execution_time_ms).
        """
        # Write script to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="sentinel_diag_",
            dir=tempfile.gettempdir(), delete=False,
        ) as f:
            f.write(script)
            script_path = f.name

        try:
            start = datetime.now()

            # Run in subprocess with restricted environment
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "PYTHONPATH": str(_PROJECT_ROOT),
                # Pass through DB access (script may need it for SELECT queries)
                "PGHOST": "localhost",
                "PGPORT": "5432",
                "PGDATABASE": "ada",
                "PGUSER": "ada",
                "PGPASSWORD": "ada_local",
                # Minimal Python environment
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }

            # Find the Python interpreter
            python = sys.executable

            result = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    python, script_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=str(_PROJECT_ROOT),
                ),
                timeout=5,  # timeout for process creation
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    result.communicate(),
                    timeout=_SCRIPT_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                result.kill()
                await result.wait()
                elapsed = int((datetime.now() - start).total_seconds() * 1000)
                return (
                    "",
                    f"TIMEOUT: Script exceeded {_SCRIPT_TIMEOUT_SEC}s limit",
                    -1,
                    elapsed,
                )

            elapsed = int((datetime.now() - start).total_seconds() * 1000)

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            return stdout, stderr, result.returncode, elapsed

        except Exception as e:
            return "", f"Execution error: {e}", -1, 0
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    async def _analyze_output(
        self, question: str, stdout: str, stderr: str,
    ) -> dict[str, Any]:
        """Ask the control LLM to interpret diagnostic output."""
        if not self.gateway:
            return self._fallback_analysis(stdout, stderr)

        errors_section = ""
        if stderr:
            errors_section = f"\nThe script also produced errors:\n```\n{stderr[:5000]}\n```"

        prompt = _ANALYSIS_SYSTEM_PROMPT.format(
            user_question=question,
            script_output=stdout[:20000],
            script_errors=errors_section,
        )

        try:
            response = await self.gateway.chat_response(
                model=self.control_model,
                messages=[
                    {"role": "system", "content": "You are a diagnostic analyst. Respond in valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                timeout=30.0,
            )
            return json.loads(response)
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"DiagnosticEngine: analysis LLM failed: {e}, using fallback")
            return self._fallback_analysis(stdout, stderr)

    def _fallback_analysis(self, stdout: str, stderr: str) -> dict[str, Any]:
        """Simple heuristic analysis when LLM is unavailable."""
        findings = []
        verdict = "INCONCLUSIVE"

        if not stdout and stderr:
            verdict = "FAIL"
            findings.append(f"Script crashed: {stderr[:200]}")
        elif "[✗]" in stdout or "FAIL" in stdout.upper():
            verdict = "FAIL"
            for line in stdout.splitlines():
                if "[✗]" in line or "FAIL" in line.upper():
                    findings.append(line.strip())
        elif "[!]" in stdout or "WARN" in stdout.upper():
            verdict = "WARN"
            for line in stdout.splitlines():
                if "[!]" in line or "WARN" in line.upper():
                    findings.append(line.strip())
        elif "[✓]" in stdout or "PASS" in stdout.upper():
            verdict = "PASS"
            findings.append("All checks passed")
        else:
            findings.append("Script produced output but verdict unclear")

        return {
            "verdict": verdict,
            "findings": findings[:10],
            "measurements": {},
            "recommendation": "Review the full script output for details.",
        }

    def _extract_code(self, response: str) -> str | None:
        """Extract Python code from LLM response (handles markdown fences)."""
        if not response:
            return None

        # Try to extract from ```python ... ``` blocks
        if "```python" in response:
            parts = response.split("```python")
            if len(parts) >= 2:
                code = parts[1].split("```")[0]
                return code.strip()

        # Try to extract from ``` ... ``` blocks
        if "```" in response:
            parts = response.split("```")
            if len(parts) >= 3:
                code = parts[1]
                # Strip language identifier if present
                if code.startswith(("python\n", "py\n")):
                    code = code.split("\n", 1)[1]
                return code.strip()

        # No fences — assume the whole response is code
        # But only if it looks like Python
        if response.strip().startswith(("import ", "from ", "async ", "def ", "#")):
            return response.strip()

        return None

    def _map_verdict(self, verdict_str: str) -> DiagnosticVerdict:
        mapping = {
            "PASS": DiagnosticVerdict.PASS,
            "WARN": DiagnosticVerdict.WARN,
            "FAIL": DiagnosticVerdict.FAIL,
            "INCONCLUSIVE": DiagnosticVerdict.INCONCLUSIVE,
        }
        return mapping.get(verdict_str.upper(), DiagnosticVerdict.INCONCLUSIVE)

    async def _persist_report(self, report: DiagnosticReport) -> None:
        """Save diagnostic report to database."""
        try:
            await self.store.pool.execute(
                """INSERT INTO sentinel_diagnostics
                   (diagnostic_id, created_at, user_question, target_subsystem,
                    script_source, harness_verdict, harness_violations,
                    executed, execution_stdout, execution_stderr,
                    execution_time_ms, execution_exit_code,
                    verdict, findings, measurements, recommendation,
                    model_used)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
                report.diagnostic_id, report.created_at, report.user_question,
                report.target_subsystem, report.script_source,
                report.harness_verdict, report.harness_violations,
                report.executed, report.execution_stdout, report.execution_stderr,
                report.execution_time_ms, report.execution_exit_code,
                report.verdict.name, report.findings,
                json.dumps(report.measurements), report.recommendation,
                report.model_used,
            )
        except Exception as e:
            log.error(f"Failed to persist diagnostic report: {e}")

    def format_report(self, report: DiagnosticReport) -> str:
        """Format a DiagnosticReport as readable text for the user."""
        lines = [
            f"=== DIAGNOSTIC REPORT: {report.diagnostic_id} ===",
            f"Question: {report.user_question}",
            f"Subsystem: {report.target_subsystem}",
            f"Verdict: {report.verdict.name}",
            f"Time: {report.created_at.isoformat()}",
            f"Execution: {report.execution_time_ms}ms, exit code {report.execution_exit_code}",
            "",
        ]

        if report.harness_violations:
            lines.append("--- HARNESS VIOLATIONS ---")
            for v in report.harness_violations:
                lines.append(f"  ✗ {v}")
            lines.append("")

        if report.findings:
            lines.append("--- FINDINGS ---")
            for i, f in enumerate(report.findings, 1):
                lines.append(f"  {i}. {f}")
            lines.append("")

        if report.measurements:
            lines.append("--- MEASUREMENTS ---")
            for k, v in report.measurements.items():
                lines.append(f"  {k}: {v}")
            lines.append("")

        if report.recommendation:
            lines.append("--- RECOMMENDATION ---")
            lines.append(f"  {report.recommendation}")
            lines.append("")

        if report.execution_stdout:
            lines.append("--- RAW OUTPUT (first 2000 chars) ---")
            lines.append(report.execution_stdout[:2000])
            lines.append("")

        return "\n".join(lines)
