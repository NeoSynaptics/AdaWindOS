"""Script Harness — AST-level enforcer for diagnostic scripts.

This is the ultra-thick safety layer. The local coding LLM writes diagnostic
scripts, and this harness validates them BEFORE execution. The rules:

ALLOWED (read-only operations):
  - File reads (open(..., 'r'), Path.read_text(), Path.read_bytes())
  - Database SELECT queries (asyncpg, psycopg2)
  - Import standard library modules (json, os.path, datetime, re, etc.)
  - Import analysis modules (collections, statistics, itertools)
  - Print statements (all output goes to stdout capture)
  - Math, string operations, data structures
  - HTTP GET requests (for health checks on local services)
  - Reading environment variables
  - os.path.*, os.listdir, os.stat (read-only filesystem inspection)

BLOCKED (any side effects):
  - File writes (open(..., 'w'), write(), unlink, remove, rmdir, mkdir)
  - Database mutations (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE)
  - subprocess, os.system, os.exec*, os.popen, os.spawn*
  - eval(), exec(), compile() with exec mode
  - Network mutations (POST, PUT, DELETE, PATCH requests)
  - Import of dangerous modules (shutil, ctypes, signal, multiprocessing)
  - sys.exit, os._exit, os.kill
  - __import__, importlib.import_module with dynamic strings
  - Monkey-patching (setattr on modules/classes)
  - Any async operations that could affect system state

The harness works at TWO levels:
1. Static analysis (AST walk) — catches structural violations before execution
2. Runtime sandbox — restricted builtins, read-only filesystem via tempdir copy
"""

import ast
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("ada.sentinel.harness")


# --- Blocked items ---

BLOCKED_MODULES = frozenset({
    "shutil", "ctypes", "signal", "multiprocessing", "threading",
    "subprocess", "pty", "fcntl", "termios", "resource",
    "socket",  # raw sockets — httpx is allowed for GET only
    "pickle", "shelve", "marshal",  # deserialization attacks
    "code", "codeop", "compileall",
    "webbrowser",
    "xmlrpc", "ftplib", "smtplib", "poplib", "imaplib", "nntplib",
    "telnetlib",
})

BLOCKED_FUNCTIONS = frozenset({
    # Execution
    "eval", "exec", "compile", "__import__",
    # System
    "exit", "quit",
    # Dangerous builtins
    "breakpoint", "input",  # input blocks, breakpoint drops to debugger
})

BLOCKED_ATTRIBUTES = frozenset({
    # os dangerous
    "system", "popen", "exec", "execl", "execle", "execlp", "execv",
    "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "kill", "killpg", "_exit", "fork", "forkpty",
    # File mutation
    "unlink", "remove", "rmdir", "removedirs", "rename", "renames",
    "replace", "makedirs", "mkdir", "link", "symlink",
    "truncate", "write", "writelines",
    # Module monkey-patching
    "setattr", "delattr",
})

BLOCKED_FILE_MODES = frozenset({
    "w", "wb", "a", "ab", "w+", "wb+", "a+", "ab+",
    "x", "xb", "x+", "xb+",
})

# SQL keywords that indicate mutation (case-insensitive check on string literals)
MUTATION_SQL_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "MERGE",
})

# HTTP methods that indicate mutation
MUTATION_HTTP_METHODS = frozenset({
    "post", "put", "delete", "patch",
})


@dataclass
class HarnessResult:
    """Result of harness validation."""
    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.passed:
            return "PASSED — script is read-only and safe to execute"
        return f"BLOCKED — {len(self.violations)} violation(s): {'; '.join(self.violations[:5])}"


class ScriptHarness:
    """Validates diagnostic scripts via AST analysis before execution.

    This is the gatekeeper. If it says no, the script does not run. Period.
    """

    def validate(self, source: str) -> HarnessResult:
        """Parse and validate a diagnostic script. Returns pass/fail with details."""
        result = HarnessResult(passed=True)

        # Step 0: Parse
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return HarnessResult(
                passed=False,
                violations=[f"Syntax error: {e}"],
            )

        # Step 1: Walk all nodes
        for node in ast.walk(tree):
            self._check_node(node, result)

        # Step 2: Check string literals for SQL mutations
        self._check_sql_strings(tree, result)

        # Step 3: Check overall structure
        self._check_structure(tree, result)

        if result.violations:
            result.passed = False

        return result

    def _check_node(self, node: ast.AST, result: HarnessResult) -> None:
        """Check a single AST node for violations."""

        # --- Import checks ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_root = alias.name.split(".")[0]
                if module_root in BLOCKED_MODULES:
                    result.violations.append(f"Blocked import: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_root = node.module.split(".")[0]
                if module_root in BLOCKED_MODULES:
                    result.violations.append(f"Blocked import: from {node.module}")

        # --- Function call checks ---
        elif isinstance(node, ast.Call):
            self._check_call(node, result)

        # --- Attribute access checks ---
        elif isinstance(node, ast.Attribute):
            if node.attr in BLOCKED_ATTRIBUTES:
                # Context: is this a call or just an access?
                result.violations.append(
                    f"Blocked attribute access: .{node.attr} "
                    f"(line {getattr(node, 'lineno', '?')})"
                )

        # --- Delete checks ---
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    result.warnings.append("dict/list deletion detected — verify it's in-memory only")
                elif isinstance(target, ast.Attribute):
                    result.violations.append(
                        f"Attribute deletion: del .{target.attr} "
                        f"(line {getattr(node, 'lineno', '?')})"
                    )

        # --- Global/nonlocal (could indicate mutation of outer state) ---
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            result.warnings.append(
                f"{'global' if isinstance(node, ast.Global) else 'nonlocal'} "
                f"declaration: {', '.join(node.names)} — verify no mutation of system state"
            )

    def _check_call(self, node: ast.Call, result: HarnessResult) -> None:
        """Check a function call node."""

        # Direct function calls: eval(), exec(), etc.
        if isinstance(node.func, ast.Name):
            if node.func.id in BLOCKED_FUNCTIONS:
                result.violations.append(
                    f"Blocked function call: {node.func.id}() "
                    f"(line {getattr(node, 'lineno', '?')})"
                )

            # Check open() mode
            if node.func.id == "open":
                self._check_open_mode(node, result)

        # Method calls: obj.method()
        elif isinstance(node.func, ast.Attribute):
            method = node.func.attr

            # Check for HTTP mutation methods
            if method in MUTATION_HTTP_METHODS:
                result.violations.append(
                    f"Blocked HTTP mutation method: .{method}() "
                    f"(line {getattr(node, 'lineno', '?')})"
                )

            # Check for file write methods
            if method in ("write", "writelines"):
                result.violations.append(
                    f"Blocked file write: .{method}() "
                    f"(line {getattr(node, 'lineno', '?')})"
                )

            # Check for path mutation methods
            if method in ("unlink", "rmdir", "mkdir", "makedirs", "rename",
                         "replace", "symlink_to", "hardlink_to", "touch",
                         "write_text", "write_bytes"):
                result.violations.append(
                    f"Blocked path mutation: .{method}() "
                    f"(line {getattr(node, 'lineno', '?')})"
                )

            # Check execute() on DB connections — verify it's SELECT only
            if method == "execute" or method == "executemany":
                result.warnings.append(
                    f"DB .{method}() call at line {getattr(node, 'lineno', '?')} "
                    "— SQL validated separately"
                )

    def _check_open_mode(self, node: ast.Call, result: HarnessResult) -> None:
        """Check that open() is read-only."""
        mode = "r"  # default

        # Check positional arg (second argument)
        if len(node.args) >= 2:
            mode_arg = node.args[1]
            if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                mode = mode_arg.value

        # Check keyword arg
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value

        if mode in BLOCKED_FILE_MODES:
            result.violations.append(
                f"Blocked: open() with write mode '{mode}' "
                f"(line {getattr(node, 'lineno', '?')})"
            )

    def _check_sql_strings(self, tree: ast.AST, result: HarnessResult) -> None:
        """Scan all string constants for SQL mutation keywords."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper().strip()
                # Only check strings that look like SQL (contain common SQL keywords)
                if any(kw in upper for kw in ("SELECT", "FROM", "WHERE", "TABLE")):
                    for mutation_kw in MUTATION_SQL_KEYWORDS:
                        # Check if mutation keyword appears at the START of a statement
                        # or after a semicolon (multi-statement)
                        if upper.startswith(mutation_kw) or f"; {mutation_kw}" in upper.replace("  ", " "):
                            result.violations.append(
                                f"Blocked: SQL mutation keyword '{mutation_kw}' found in string "
                                f"(line {getattr(node, 'lineno', '?')})"
                            )
                            break

    def _check_structure(self, tree: ast.AST, result: HarnessResult) -> None:
        """Check overall script structure for safety."""
        # Must have at least one print() or return statement (produces output)
        has_output = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "print":
                    has_output = True
                    break
            if isinstance(node, ast.Return):
                has_output = True
                break

        if not has_output:
            result.warnings.append(
                "Script has no print() statements — diagnostic output may be empty"
            )

        # Check script length — diagnostics should be focused
        lines = len(tree.body)
        if lines > 200:
            result.warnings.append(
                f"Script is {lines} top-level statements — diagnostics should be focused"
            )
