"""FORGE pipeline tests — context management, tool execution, and repo grounding.

Tests the dispatcher's repo-grounded coding pipeline without requiring
a running Ollama instance (mocked LLM calls).
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ada.agents.dispatcher import (
    _git_context,
    _file_tree,
    _read_instruction_files,
    _execute_tool,
    _estimate_tokens,
    _estimate_messages_tokens,
    _summarize_old_messages,
    _truncate_output,
    _usable_input_budget,
    FORGE_TOOLS,
    READ_FILE_MAX_LINES,
    READ_FILE_HARD_CAP_CHARS,
    RUN_COMMAND_STDOUT_CAP,
    GREP_MAX_LINES,
    CONTEXT_WINDOW_TOKENS,
    OUTPUT_RESERVE_TOKENS,
    SUMMARIZE_THRESHOLD,
    HARD_CAP_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_repo(tmp_path):
    """Create a temp directory that looks like a git repo with files."""
    # Init git
    os.system(f"cd {tmp_path} && git init -q && git config user.email test@test.com && git config user.name Test")

    # Create some files
    (tmp_path / "main.py").write_text('def hello():\n    print("hello")\n')
    (tmp_path / "utils.py").write_text('def add(a, b):\n    return a + b\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text(
        'from main import hello\ndef test_hello():\n    hello()\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text('class Core:\n    pass\n')

    # Commit everything
    os.system(f"cd {tmp_path} && git add -A && git commit -q -m 'initial'")

    return tmp_path


@pytest.fixture
def large_file(tmp_path):
    """Create a large Python file for testing read limits."""
    lines = [f"line_{i} = {i}" for i in range(500)]
    (tmp_path / "big.py").write_text("\n".join(lines))
    return tmp_path


# ---------------------------------------------------------------------------
# Token estimator tests
# ---------------------------------------------------------------------------

class TestTokenEstimator:
    def test_basic_estimate(self):
        # 3.5 chars/token → 100 chars ≈ 28 tokens
        assert 25 <= _estimate_tokens("x" * 100) <= 35

    def test_empty_string(self):
        assert _estimate_tokens("") == 0

    def test_messages_tokens(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello world"},
        ]
        total = _estimate_messages_tokens(msgs)
        assert total > 0
        assert total < 50  # these are tiny messages

    def test_messages_with_tool_calls(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path": "main.py"}'}}
            ]},
        ]
        total = _estimate_messages_tokens(msgs)
        assert total > 0

    def test_usable_budget(self):
        budget = _usable_input_budget()
        assert budget == CONTEXT_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS


# ---------------------------------------------------------------------------
# Summarization tests
# ---------------------------------------------------------------------------

class TestSummarization:
    def test_no_summarize_short_conversations(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "done"},
        ]
        result = _summarize_old_messages(msgs, keep_last=6)
        assert result == msgs  # unchanged

    def test_summarize_compresses_old_tool_calls(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        # Add 20 tool interactions
        for i in range(10):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": json.dumps({"path": f"file_{i}.py"})}}
            ]})
            msgs.append({"role": "tool", "tool_call_id": f"tc_{i}", "content": f"line 1\nline 2\nline 3"})

        result = _summarize_old_messages(msgs, keep_last=6)
        assert len(result) < len(msgs)
        # Should have: system + user + summary + last 6
        assert len(result) == 3 + 6
        # Summary should mention files read
        summary = result[2]["content"]
        assert "Files read" in summary

    def test_summarize_keeps_errors(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path": "missing.py"}'}}
            ]},
            {"role": "tool", "tool_call_id": "tc_1", "content": "ERROR: File not found: missing.py"},
            # Recent messages (kept)
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "write_file", "arguments": '{"path": "new.py", "content": "x"}'}}
            ]},
            {"role": "tool", "tool_call_id": "tc_2", "content": "OK: wrote 1 bytes"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "write_file", "arguments": '{"path": "new2.py", "content": "y"}'}}
            ]},
            {"role": "tool", "tool_call_id": "tc_3", "content": "OK: wrote 1 bytes"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "write_file", "arguments": '{"path": "new3.py", "content": "z"}'}}
            ]},
            {"role": "tool", "tool_call_id": "tc_4", "content": "OK: wrote 1 bytes"},
            {"role": "assistant", "content": "DONE"},
        ]
        result = _summarize_old_messages(msgs, keep_last=6)
        summary = result[2]["content"]
        assert "Tool error" in summary


# ---------------------------------------------------------------------------
# Truncation tests
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_no_truncation_short_text(self):
        text = "short"
        assert _truncate_output(text, 100) == text

    def test_truncation_preserves_start_and_end(self):
        text = "START" + "x" * 1000 + "END"
        result = _truncate_output(text, 100)
        assert result.startswith("START")
        assert result.endswith("END")
        assert "truncated" in result

    def test_truncation_shows_char_count(self):
        text = "a" * 500
        result = _truncate_output(text, 100)
        assert "400 chars" in result


# ---------------------------------------------------------------------------
# Tool execution tests (real filesystem)
# ---------------------------------------------------------------------------

class TestToolExecution:
    def test_read_file(self, temp_repo):
        result = _execute_tool("read_file", {"path": "main.py"}, str(temp_repo))
        assert "hello" in result
        assert result.startswith("1\t")  # line numbers

    def test_read_file_with_offset(self, temp_repo):
        result = _execute_tool("read_file", {"path": "main.py", "offset": 1}, str(temp_repo))
        assert "print" in result
        assert not result.startswith("1\t")  # starts from line 2

    def test_read_file_respects_limit(self, large_file):
        result = _execute_tool("read_file", {"path": "big.py"}, str(large_file))
        lines = result.strip().splitlines()
        # Should be capped at READ_FILE_MAX_LINES (200) + overflow notice
        assert len(lines) <= READ_FILE_MAX_LINES + 2  # +2 for truncation notice + more lines notice

    def test_read_file_not_found(self, temp_repo):
        result = _execute_tool("read_file", {"path": "nope.py"}, str(temp_repo))
        assert "ERROR" in result

    def test_read_file_path_escape(self, temp_repo):
        result = _execute_tool("read_file", {"path": "../../etc/passwd"}, str(temp_repo))
        assert "ERROR" in result

    def test_write_file(self, temp_repo):
        result = _execute_tool("write_file", {"path": "new.py", "content": "x = 1"}, str(temp_repo))
        assert "OK" in result
        assert (temp_repo / "new.py").read_text() == "x = 1"

    def test_write_file_nested(self, temp_repo):
        result = _execute_tool("write_file", {"path": "deep/nested/file.py", "content": "y = 2"}, str(temp_repo))
        assert "OK" in result
        assert (temp_repo / "deep" / "nested" / "file.py").read_text() == "y = 2"

    def test_write_file_path_escape(self, temp_repo):
        result = _execute_tool("write_file", {"path": "../../evil.py", "content": "bad"}, str(temp_repo))
        assert "ERROR" in result

    def test_edit_file(self, temp_repo):
        result = _execute_tool("edit_file", {
            "path": "main.py",
            "old_string": 'print("hello")',
            "new_string": 'print("goodbye")',
        }, str(temp_repo))
        assert "OK" in result
        assert 'print("goodbye")' in (temp_repo / "main.py").read_text()

    def test_edit_file_not_found_string(self, temp_repo):
        result = _execute_tool("edit_file", {
            "path": "main.py",
            "old_string": "DOES NOT EXIST",
            "new_string": "replacement",
        }, str(temp_repo))
        assert "ERROR" in result
        assert "not found" in result

    def test_grep_search(self, temp_repo):
        result = _execute_tool("grep_search", {"pattern": "def ", "glob": "*.py"}, str(temp_repo))
        assert "hello" in result or "add" in result

    def test_list_files(self, temp_repo):
        result = _execute_tool("list_files", {"pattern": "**/*.py"}, str(temp_repo))
        assert "main.py" in result

    def test_run_command(self, temp_repo):
        result = _execute_tool("run_command", {"command": "echo hello"}, str(temp_repo))
        assert "hello" in result
        assert "exit code: 0" in result

    def test_run_command_timeout(self, temp_repo):
        result = _execute_tool("run_command", {"command": "sleep 200", "timeout": 1}, str(temp_repo))
        assert "ERROR" in result or "timed out" in result

    def test_run_command_stdout_cap(self, temp_repo):
        # Generate lots of output
        result = _execute_tool("run_command", {
            "command": f"python3 -c \"print('x' * 10000)\"",
        }, str(temp_repo))
        # Should be truncated
        assert len(result) < 10000

    def test_unknown_tool(self, temp_repo):
        result = _execute_tool("fake_tool", {}, str(temp_repo))
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# Git context tests
# ---------------------------------------------------------------------------

class TestGitContext:
    def test_git_context_real_repo(self, temp_repo):
        ctx = _git_context(str(temp_repo))
        assert "git status" in ctx
        assert "recent commits" in ctx
        assert "initial" in ctx  # our commit message

    def test_git_context_not_a_repo(self, tmp_path):
        ctx = _git_context(str(tmp_path))
        assert "not a git repository" in ctx

    def test_git_context_empty_path(self):
        ctx = _git_context("")
        assert "not a git repository" in ctx

    def test_file_tree(self, temp_repo):
        tree = _file_tree(str(temp_repo))
        assert "main.py" in tree
        assert "utils.py" in tree
        assert "src/core.py" in tree

    def test_instruction_files(self, temp_repo):
        # No instruction files → empty
        assert _read_instruction_files(str(temp_repo)) == ""

        # Create one
        (temp_repo / "CLAUDE.md").write_text("# Project rules\nUse pytest.")
        result = _read_instruction_files(str(temp_repo))
        assert "Use pytest" in result


# ---------------------------------------------------------------------------
# FORGE dispatcher integration test (mocked LLM)
# ---------------------------------------------------------------------------

class TestForgeDispatcher:
    @pytest.mark.asyncio
    async def test_dispatch_requires_repo_path(self):
        """FORGE dispatch without repo_path should fail immediately."""
        from ada.agents.dispatcher import AgentDispatcher

        dispatcher = AgentDispatcher()
        result = await dispatcher.dispatch({
            "task_id": "tsk_test_001",
            "agent": "FORGE",
            "objective": "Add logging",
            # No repo_path!
        })
        assert result["status"] == "failed"
        assert "repo_path" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_with_invalid_repo_path(self):
        """FORGE dispatch with non-existent path should fail in plan node."""
        from ada.agents.dispatcher import AgentDispatcher

        dispatcher = AgentDispatcher()
        result = await dispatcher.dispatch({
            "task_id": "tsk_test_002",
            "agent": "FORGE",
            "objective": "Add logging",
            "repo_path": "/nonexistent/path",
        })
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    def test_tool_count(self):
        assert len(FORGE_TOOLS) == 6

    def test_budget_thresholds_ordered(self):
        assert SUMMARIZE_THRESHOLD < HARD_CAP_THRESHOLD < 1.0

    def test_output_caps_reasonable(self):
        assert READ_FILE_MAX_LINES <= 300
        assert READ_FILE_HARD_CAP_CHARS <= 10_000
        assert RUN_COMMAND_STDOUT_CAP <= 3_000
        assert GREP_MAX_LINES <= 60
