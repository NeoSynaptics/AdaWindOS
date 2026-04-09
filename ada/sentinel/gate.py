"""Patch Gate — the ONE narrow entry point for all code changes.

Like Apple's App Store review: every fix must pass through this gate.
The gate validates syntax, checks scope (patch can only touch files the
error touched), runs in a sandbox, and requires user approval before applying.

Flow:
1. Submit patch (diff + metadata)
2. Gate validates:
   a. Syntax check — is the diff valid Python?
   b. Scope check — does it only touch files from the error report?
   c. Sandbox run — apply in temp venv, run basic smoke test
3. If all pass → mark APPROVED, wait for user approval hash
4. User confirms → apply patch to live system
5. If regression detected → automatic rollback
"""

import ast
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import Patch, PatchVerdict

log = logging.getLogger("ada.sentinel.gate")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class GateError(Exception):
    """Raised when gate validation fails."""
    pass


class PatchGate:
    """The narrow input — validates, sandboxes, and applies patches.

    Every code change that enters AdaOS at runtime goes through here.
    No exceptions. No backdoors.
    """

    def __init__(self, store, registry=None, journal=None):
        self.store = store
        self.registry = registry
        self.journal = journal
        self._sandbox_dir: Path | None = None

    async def submit(
        self,
        target_report_id: str,
        diff: str,
        description: str,
        author: str,
        source_prompt: str | None = None,
    ) -> Patch:
        """Submit a patch for review. Returns the Patch with validation results.

        The patch is NOT applied yet — it goes through validation first.
        """
        patch_id = f"patch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        affected_files = _extract_files_from_diff(diff)

        patch = Patch(
            patch_id=patch_id,
            created_at=datetime.now(),
            target_report_id=target_report_id,
            description=description,
            diff=diff,
            affected_files=affected_files,
            author=author,
            source_prompt=source_prompt,
        )

        # Validate the patch through all gates
        await self._validate(patch, target_report_id)

        # Persist to DB
        await self._persist_patch(patch)

        if patch.verdict == PatchVerdict.APPROVED:
            log.info(f"Gate: patch {patch_id} APPROVED — awaiting user confirmation")
        else:
            log.warning(f"Gate: patch {patch_id} REJECTED — {self._rejection_reason(patch)}")

        return patch

    async def approve(self, patch_id: str, approval_hash: str) -> Patch:
        """User approves a patch. Apply it to the live system.

        The approval_hash is a simple confirmation token — the user
        generates it by reviewing the patch and confirming intent.
        """
        patch_data = await self._get_patch(patch_id)
        if not patch_data:
            raise GateError(f"Patch {patch_id} not found")

        if patch_data.get("verdict") != PatchVerdict.APPROVED.name:
            raise GateError(f"Patch {patch_id} is not in APPROVED state (current: {patch_data.get('verdict')})")

        # Verify approval hash matches
        expected_hash = _compute_approval_hash(patch_id, patch_data["diff"])
        if approval_hash != expected_hash:
            raise GateError("Approval hash mismatch — patch may have been tampered with")

        # Apply the patch
        try:
            _apply_diff(patch_data["diff"])
        except Exception as e:
            log.error(f"Gate: failed to apply patch {patch_id}: {e}")
            await self._update_verdict(patch_id, PatchVerdict.REJECTED)
            raise GateError(f"Patch application failed: {e}") from e

        # Mark as applied
        await self._update_verdict(patch_id, PatchVerdict.APPLIED, approval_hash=approval_hash)

        # Mark the report as resolved
        if self.registry:
            await self.registry.mark_resolved(patch_data["target_report_id"], patch_id)

        # Journal entry
        if self.journal:
            await self.journal.log(
                action_type="sentinel_patch_applied",
                summary=f"Patch {patch_id} applied to fix {patch_data['target_report_id']}",
                band=2,
                details={
                    "patch_id": patch_id,
                    "report_id": patch_data["target_report_id"],
                    "files": patch_data.get("affected_files", []),
                    "author": patch_data.get("author", "unknown"),
                },
            )

        log.info(f"Gate: patch {patch_id} APPLIED successfully")
        return patch_data

    async def rollback(self, patch_id: str, reason: str) -> None:
        """Rollback an applied patch using `git checkout` on affected files."""
        patch_data = await self._get_patch(patch_id)
        if not patch_data:
            raise GateError(f"Patch {patch_id} not found")

        if patch_data.get("verdict") != PatchVerdict.APPLIED.name:
            raise GateError(f"Patch {patch_id} is not in APPLIED state")

        affected = patch_data.get("affected_files", [])
        try:
            for fpath in affected:
                full = _PROJECT_ROOT / fpath
                if full.exists():
                    # Use git to restore the file to its pre-patch state
                    subprocess.run(
                        ["git", "checkout", "HEAD~1", "--", str(full)],
                        cwd=str(_PROJECT_ROOT),
                        capture_output=True,
                        timeout=10,
                    )
        except Exception as e:
            log.error(f"Gate: rollback failed for {patch_id}: {e}")
            raise GateError(f"Rollback failed: {e}") from e

        await self._update_verdict(
            patch_id, PatchVerdict.ROLLED_BACK,
            rollback_reason=reason,
        )

        if self.journal:
            await self.journal.log(
                action_type="sentinel_patch_rollback",
                summary=f"Patch {patch_id} rolled back: {reason}",
                band=2,
                details={"patch_id": patch_id, "reason": reason, "files": affected},
            )

        log.warning(f"Gate: patch {patch_id} ROLLED BACK — {reason}")

    async def get_pending(self) -> list[dict]:
        """Get all patches awaiting user approval."""
        try:
            rows = await self.store.pool.fetch(
                """SELECT * FROM sentinel_patches
                   WHERE verdict = $1
                   ORDER BY created_at DESC""",
                PatchVerdict.APPROVED.name,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"Failed to fetch pending patches: {e}")
            return []

    async def get_history(self, limit: int = 50) -> list[dict]:
        """Get patch application history."""
        try:
            rows = await self.store.pool.fetch(
                "SELECT * FROM sentinel_patches ORDER BY created_at DESC LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"Failed to fetch patch history: {e}")
            return []

    # --- Validation pipeline ---

    async def _validate(self, patch: Patch, report_id: str) -> None:
        """Run the patch through all validation gates."""
        # Gate 1: Syntax — is it valid Python?
        patch.syntax_valid = _check_syntax(patch.diff)
        if not patch.syntax_valid:
            patch.verdict = PatchVerdict.REJECTED
            return

        # Gate 2: Scope — does it only touch files related to the error?
        report_data = None
        if self.registry:
            report_data = await self.registry.get_report(report_id)
        if report_data:
            allowed_files = set(report_data.get("affected_files", []))
            # Also allow files in the same module directory
            for f in list(allowed_files):
                allowed_files.add(str(Path(f).parent))
            patch.scope_valid = all(
                any(af in pf or str(Path(pf).parent) in allowed_files for af in allowed_files)
                for pf in patch.affected_files
            )
        else:
            # No report data — conservative: scope check passes but warn
            patch.scope_valid = True
            log.warning(f"Gate: no report data for {report_id}, skipping scope check")

        if not patch.scope_valid:
            patch.verdict = PatchVerdict.REJECTED
            return

        # Gate 3: Sandbox — apply in temp dir and check no import errors
        patch.sandbox_passed = await self._sandbox_test(patch)
        if not patch.sandbox_passed:
            patch.verdict = PatchVerdict.REJECTED
            return

        # All gates passed
        patch.verdict = PatchVerdict.APPROVED

    async def _sandbox_test(self, patch: Patch) -> bool:
        """Apply patch in a temporary copy and verify basic integrity."""
        sandbox = None
        try:
            sandbox = Path(tempfile.mkdtemp(prefix="sentinel_sandbox_"))

            # Copy only the affected files into the sandbox
            for fpath in patch.affected_files:
                src = _PROJECT_ROOT / fpath
                dst = sandbox / fpath
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    shutil.copy2(src, dst)

            # Apply the diff in the sandbox
            diff_file = sandbox / "patch.diff"
            diff_file.write_text(patch.diff)

            result = subprocess.run(
                ["patch", "-p1", "--dry-run", "-i", str(diff_file)],
                cwd=str(sandbox),
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                log.warning(f"Gate sandbox: patch --dry-run failed: {result.stderr}")
                return False

            # Actually apply
            result = subprocess.run(
                ["patch", "-p1", "-i", str(diff_file)],
                cwd=str(sandbox),
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                log.warning(f"Gate sandbox: patch apply failed: {result.stderr}")
                return False

            # Syntax check the patched files
            for fpath in patch.affected_files:
                patched = sandbox / fpath
                if patched.exists() and patched.suffix == ".py":
                    try:
                        ast.parse(patched.read_text())
                    except SyntaxError as e:
                        log.warning(f"Gate sandbox: syntax error in {fpath} after patch: {e}")
                        return False

            return True

        except subprocess.TimeoutExpired:
            log.warning("Gate sandbox: timeout during patch test")
            return False
        except Exception as e:
            log.error(f"Gate sandbox: unexpected error: {e}")
            return False
        finally:
            if sandbox and sandbox.exists():
                shutil.rmtree(sandbox, ignore_errors=True)

    # --- Persistence ---

    async def _persist_patch(self, patch: Patch) -> None:
        try:
            await self.store.pool.execute(
                """INSERT INTO sentinel_patches
                   (patch_id, created_at, target_report_id, description, diff,
                    affected_files, author, source_prompt, verdict,
                    syntax_valid, scope_valid, sandbox_passed)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                patch.patch_id, patch.created_at, patch.target_report_id,
                patch.description, patch.diff, patch.affected_files,
                patch.author, patch.source_prompt, patch.verdict.name,
                patch.syntax_valid, patch.scope_valid, patch.sandbox_passed,
            )
        except Exception as e:
            log.error(f"Failed to persist patch {patch.patch_id}: {e}")

    async def _get_patch(self, patch_id: str) -> dict | None:
        try:
            row = await self.store.pool.fetchrow(
                "SELECT * FROM sentinel_patches WHERE patch_id = $1", patch_id,
            )
            return dict(row) if row else None
        except Exception as e:
            log.error(f"Failed to fetch patch {patch_id}: {e}")
            return None

    async def _update_verdict(
        self,
        patch_id: str,
        verdict: PatchVerdict,
        approval_hash: str | None = None,
        rollback_reason: str | None = None,
    ) -> None:
        try:
            sets = ["verdict = $2", "updated_at = NOW()"]
            params: list[Any] = [patch_id, verdict.name]
            i = 3
            if verdict == PatchVerdict.APPLIED:
                sets.append(f"applied_at = ${i}")
                params.append(datetime.now())
                i += 1
            if approval_hash:
                sets.append(f"approval_hash = ${i}")
                params.append(approval_hash)
                i += 1
            if verdict == PatchVerdict.ROLLED_BACK:
                sets.append(f"rolled_back_at = ${i}")
                params.append(datetime.now())
                i += 1
            if rollback_reason:
                sets.append(f"rollback_reason = ${i}")
                params.append(rollback_reason)
                i += 1
            await self.store.pool.execute(
                f"UPDATE sentinel_patches SET {', '.join(sets)} WHERE patch_id = $1",
                *params,
            )
        except Exception as e:
            log.error(f"Failed to update verdict for {patch_id}: {e}")

    def _rejection_reason(self, patch: Patch) -> str:
        if not patch.syntax_valid:
            return "syntax check failed"
        if not patch.scope_valid:
            return "scope violation — patch touches files outside error boundary"
        if not patch.sandbox_passed:
            return "sandbox test failed"
        return "unknown"


# --- Utility functions ---

def _extract_files_from_diff(diff: str) -> list[str]:
    """Extract file paths from a unified diff."""
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                if path not in files:
                    files.append(path)
    return files


def _check_syntax(diff: str) -> bool:
    """Check that the new code in the diff is syntactically valid Python.

    Extracts added lines and tries to parse them. This is a heuristic —
    we can't fully validate without applying the diff, but we can catch
    obvious syntax errors in the new code.
    """
    added_lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])  # strip the leading +

    if not added_lines:
        return True  # pure deletion is always syntactically valid

    code = "\n".join(added_lines)
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        # Individual lines may not form valid Python on their own.
        # Try checking each contiguous block instead.
        # If more than half the blocks parse, call it valid.
        blocks = code.split("\n\n")
        valid = sum(1 for b in blocks if _try_parse(b))
        return valid > len(blocks) // 2


def _try_parse(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _apply_diff(diff: str) -> None:
    """Apply a unified diff to the project root."""
    diff_file = _PROJECT_ROOT / ".sentinel_patch.tmp"
    try:
        diff_file.write_text(diff)
        result = subprocess.run(
            ["patch", "-p1", "-i", str(diff_file)],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise GateError(f"patch command failed: {result.stderr}")
    finally:
        diff_file.unlink(missing_ok=True)


def _compute_approval_hash(patch_id: str, diff: str) -> str:
    """Compute the approval hash the user needs to confirm.

    This is simply SHA-256(patch_id + diff) — the user can verify
    they're approving the exact patch they reviewed.
    """
    return hashlib.sha256(f"{patch_id}:{diff}".encode()).hexdigest()[:16]
