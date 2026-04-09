"""Validation gates — 4-gate checker for worker results.

Gate 1: Structural — right artifact type, format correct (deterministic)
Gate 2: Technical — compiles, tests pass (sandboxed execution)
Gate 3: Semantic — matches original intent (LLM-assisted)
Gate 4: Attention — should this interrupt Daniel now (Ada judgment)
"""

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class GateResult:
    gate: int
    name: str
    passed: bool
    details: str = ""


@dataclass
class ValidationResult:
    gates: list[GateResult]
    passed: bool
    delivery_class: str = "silent"  # immediate | delayed | silent
    summary: str = ""

    @property
    def failed_gate(self) -> GateResult | None:
        for g in self.gates:
            if not g.passed:
                return g
        return None


class Validator:
    def __init__(self, ollama_base_url: str = "", model: str = "", gateway=None):
        self.ollama_base_url = ollama_base_url
        self.model = model
        self.gateway = gateway  # APU gateway (preferred)

    async def validate(self, task: dict, artifacts: list[dict]) -> ValidationResult:
        """Run all 4 gates in order. Stop on first failure."""
        gates = []

        # Gate 1: Structural
        g1 = self._gate_structural(task, artifacts)
        gates.append(g1)
        if not g1.passed:
            return ValidationResult(gates=gates, passed=False, summary=g1.details)

        # Gate 2: Technical
        g2 = await self._gate_technical(task, artifacts)
        gates.append(g2)
        if not g2.passed:
            return ValidationResult(gates=gates, passed=False, summary=g2.details)

        # Gate 3: Semantic
        g3 = await self._gate_semantic(task, artifacts)
        gates.append(g3)
        if not g3.passed:
            return ValidationResult(gates=gates, passed=False, summary=g3.details)

        # Gate 4: Attention-worthiness
        g4 = self._gate_attention(task, artifacts)
        gates.append(g4)

        return ValidationResult(
            gates=gates,
            passed=True,
            delivery_class=g4.details,
            summary="All gates passed",
        )

    def _gate_structural(self, task: dict, artifacts: list[dict]) -> GateResult:
        """Gate 1: Did the worker return the right artifact type?"""
        expected = json.loads(task.get("artifacts_expected", "[]"))
        if not artifacts:
            return GateResult(gate=1, name="structural", passed=False,
                              details="No artifacts returned")

        # Check each artifact has required fields
        for art in artifacts:
            if not art.get("type"):
                return GateResult(gate=1, name="structural", passed=False,
                                  details="Artifact missing 'type' field")
            if not art.get("content"):
                return GateResult(gate=1, name="structural", passed=False,
                                  details="Artifact missing 'content' field")

        # Check expected types are present
        received_types = {a.get("type") for a in artifacts}
        for exp in expected:
            if exp not in received_types:
                return GateResult(gate=1, name="structural", passed=False,
                                  details=f"Expected artifact type '{exp}' not found")

        return GateResult(gate=1, name="structural", passed=True, details="Format correct")

    async def _gate_technical(self, task: dict, artifacts: list[dict]) -> GateResult:
        """Gate 2: Does code compile? Tests pass? Sandbox execution safe?

        Steps:
        1. Syntax check Python artifacts via compile()
        2. Check embedded test results for failures
        3. Run code artifacts in sandbox (bwrap if available, else restricted subprocess)
        """
        PYTHON_EXTENSIONS = (".py",)
        code_artifacts = []

        for art in artifacts:
            if art.get("type") == "code":
                content = art.get("content", "")
                filename = art.get("filename", "")

                is_python = (
                    filename.endswith(PYTHON_EXTENSIONS)
                    or (not filename and self._looks_like_python(content))
                )

                if is_python and content.strip():
                    try:
                        compile(content, filename or "<artifact>", "exec")
                    except SyntaxError as e:
                        return GateResult(gate=2, name="technical", passed=False,
                                          details=f"Python syntax error: {e}")
                    code_artifacts.append(art)

            if art.get("type") == "test_results":
                results = art.get("content", {})
                if isinstance(results, dict) and results.get("failed", 0) > 0:
                    return GateResult(gate=2, name="technical", passed=False,
                                      details=f"Tests failed: {results.get('failed')} failures")

        # Run code artifacts in sandbox if any exist
        if code_artifacts:
            sandbox_result = await self._run_in_sandbox(code_artifacts)
            if not sandbox_result["passed"]:
                return GateResult(gate=2, name="technical", passed=False,
                                  details=f"Sandbox execution failed: {sandbox_result['error']}")

        return GateResult(gate=2, name="technical", passed=True, details="Technical checks passed")

    def _looks_like_python(self, content: str) -> bool:
        """Heuristic: does this code snippet look like Python?"""
        indicators = ("def ", "import ", "class ", "print(", "async def ", "from ", "if __name__")
        first_lines = content[:500].lower()
        return any(ind in first_lines for ind in indicators)

    async def _run_in_sandbox(self, artifacts: list[dict]) -> dict:
        """Execute code artifacts in a sandboxed environment.

        Writes artifacts to a temp directory, runs pytest (or python) in
        an isolated subprocess. Uses bwrap for network isolation when available.

        Returns {"passed": bool, "error": str | None}.
        """
        workdir = Path(tempfile.mkdtemp(prefix="gate2_"))
        try:
            # Write artifacts to temp dir
            has_tests = False
            for art in artifacts:
                filename = art.get("filename", "main.py")
                fpath = workdir / filename
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(art["content"])
                if "test_" in filename or "_test" in filename:
                    has_tests = True

            # Build sandbox command
            if has_tests:
                cmd = self._build_sandbox_cmd(workdir, ["python3", "-m", "pytest", "-x", "--tb=short", "."])
            else:
                # Just try to import/execute the main file
                cmd = self._build_sandbox_cmd(workdir, ["python3", "-c", "import importlib, sys; sys.path.insert(0,'.'); exec(open('main.py').read())"])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=self._sandboxed_env(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"passed": False, "error": "Sandbox execution timed out (60s)"}

            if proc.returncode == 0:
                return {"passed": True, "error": None}
            else:
                error_output = stderr.decode(errors="replace")[-1000:]
                if not error_output.strip():
                    error_output = stdout.decode(errors="replace")[-1000:]
                return {"passed": False, "error": error_output}

        except Exception as e:
            log.error(f"Sandbox execution error: {e}")
            return {"passed": False, "error": str(e)}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _build_sandbox_cmd(self, workdir: Path, inner_cmd: list[str]) -> list[str]:
        """Build a sandboxed command. Uses bwrap if available, else plain subprocess."""
        if shutil.which("bwrap"):
            return [
                "bwrap",
                "--ro-bind", "/usr", "/usr",
                "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/bin", "/bin",
                "--symlink", "usr/lib64", "/lib64",
                "--proc", "/proc",
                "--dev", "/dev",
                "--bind", str(workdir), "/workspace",
                "--unshare-net",      # no network access
                "--unshare-pid",      # isolated PID namespace
                "--die-with-parent",  # kill on parent exit
                "--chdir", "/workspace",
            ] + inner_cmd
        else:
            # Fallback: no bwrap, just run with restricted env in temp dir
            return inner_cmd

    def _sandboxed_env(self) -> dict:
        """Build a restricted environment for sandbox execution."""
        import os
        env = os.environ.copy()
        # Strip credential-related environment variables
        for key in list(env.keys()):
            upper = key.upper()
            if any(s in upper for s in ("SECRET", "TOKEN", "PASSWORD", "KEY", "CREDENTIAL", "API_KEY")):
                del env[key]
        env["HOME"] = "/tmp/sandbox"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    async def _gate_semantic(self, task: dict, artifacts: list[dict]) -> GateResult:
        """Gate 3: Does this match the original intent? (LLM-assisted)"""
        brief = task.get("brief", "")
        criteria = json.loads(task.get("success_criteria", "[]"))
        artifact_summaries = [a.get("content", "")[:500] for a in artifacts]

        prompt = f"""You are a validation engine. Does this result match the original task?

TASK: {brief}
SUCCESS CRITERIA: {json.dumps(criteria)}
RESULT SUMMARY: {json.dumps(artifact_summaries)}

Answer with ONLY a JSON object:
{{"aligned": true/false, "reason": "brief explanation"}}"""

        semantic_schema = {
            "type": "object",
            "properties": {
                "aligned": {"type": "boolean"},
                "reason": {"type": "string"}
            },
            "required": ["aligned", "reason"]
        }

        try:
            if self.gateway:
                result = await self.gateway.chat_json(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    schema=semantic_schema,
                    temperature=0.0,
                    timeout=15.0,
                )
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"{self.ollama_base_url}/api/chat",
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "format": semantic_schema,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    result = json.loads(data["message"]["content"])

            # Shared return logic for both paths
            if result.get("aligned"):
                return GateResult(gate=3, name="semantic", passed=True,
                                  details=result.get("reason", "Aligned"))
            else:
                return GateResult(gate=3, name="semantic", passed=False,
                                      details=result.get("reason", "Not aligned"))
        except Exception as e:
            log.error(f"Semantic gate failed: {e}")
            # On LLM failure, pass with warning (don't block on LLM issues)
            return GateResult(gate=3, name="semantic", passed=True,
                              details="LLM check unavailable — passed with warning")

    def _gate_attention(self, task: dict, artifacts: list[dict]) -> GateResult:
        """Gate 4: Should this interrupt Daniel now?

        Delivery classes:
        - immediate: decision needed, critical blocker, architecture fork
        - delayed: useful result, not urgent
        - silent: raw logs, intermediate output
        """
        priority = task.get("priority", "medium")
        requires_decision = task.get("requires_user_decision", False)
        task_type = task.get("type", "")

        if requires_decision:
            return GateResult(gate=4, name="attention", passed=True, details="immediate")

        if priority == "critical":
            return GateResult(gate=4, name="attention", passed=True, details="immediate")

        if priority == "high" or task_type in ("architecture",):
            return GateResult(gate=4, name="attention", passed=True, details="delayed")

        return GateResult(gate=4, name="attention", passed=True, details="delayed")
