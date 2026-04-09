"""Tests for Ada tool handlers — file ops, bash safety, memory ops."""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from ada.tools.handlers import ToolHandlers, BLOCKED_COMMANDS


@pytest.fixture
def handlers(mock_store):
    return ToolHandlers(store=mock_store)


@pytest.fixture
def tmp_file():
    """Create a temp file for read/write tests."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    f.write("hello world\nline two\n")
    f.close()
    yield f.name
    os.unlink(f.name)


class TestReadFile:

    @pytest.mark.asyncio
    async def test_read_existing_file(self, handlers, tmp_file):
        result = await handlers.read_file(path=tmp_file)
        assert "content" in result
        assert "hello world" in result["content"]

    @pytest.mark.asyncio
    async def test_read_missing_file(self, handlers):
        result = await handlers.read_file(path="/nonexistent/file.txt")
        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_read_directory_fails(self, handlers):
        result = await handlers.read_file(path="/tmp")
        assert "error" in result


class TestWriteFile:

    @pytest.mark.asyncio
    async def test_write_new_file(self, handlers):
        path = tempfile.mktemp(suffix=".txt")
        try:
            result = await handlers.write_file(path=path, content="test content")
            assert result.get("written")
            assert Path(path).read_text() == "test content"
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestEditFile:

    @pytest.mark.asyncio
    async def test_edit_replaces_pattern(self, handlers, tmp_file):
        result = await handlers.edit_file(path=tmp_file, old_string="hello", new_string="goodbye")
        assert result.get("edited")
        assert "goodbye world" in Path(tmp_file).read_text()

    @pytest.mark.asyncio
    async def test_edit_missing_pattern(self, handlers, tmp_file):
        result = await handlers.edit_file(path=tmp_file, old_string="nonexistent", new_string="x")
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestBash:

    @pytest.mark.asyncio
    async def test_simple_command(self, handlers):
        result = await handlers.bash(command="echo hello")
        assert result.get("returncode") == 0
        assert "hello" in result.get("stdout", "")

    @pytest.mark.asyncio
    async def test_blocked_command(self, handlers):
        result = await handlers.bash(command="sudo rm -rf /")
        assert "error" in result
        assert "blocked" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_timeout(self, handlers):
        result = await handlers.bash(command="sleep 10", timeout=1)
        assert "error" in result
        assert "timeout" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_credentials_stripped(self, handlers):
        # Verify credential env vars are stripped
        os.environ["TEST_SECRET_KEY"] = "should_be_gone"
        try:
            result = await handlers.bash(command="echo $TEST_SECRET_KEY")
            assert result.get("returncode") == 0
            # The secret should NOT appear in output
            assert "should_be_gone" not in result.get("stdout", "")
        finally:
            del os.environ["TEST_SECRET_KEY"]


class TestMemoryOps:

    @pytest.mark.asyncio
    async def test_memory_query_returns_list(self, handlers, mock_store):
        mock_store.search_memories = AsyncMock(return_value=[
            {"content": "test fact", "memory_type": "fact", "confidence": 0.9},
        ])
        result = await handlers.memory_query(query="test")
        assert len(result["memories"]) == 1
        assert result["memories"][0]["content"] == "test fact"

    @pytest.mark.asyncio
    async def test_memory_store(self, handlers, mock_store):
        result = await handlers.memory_store(content="new fact", memory_type="fact")
        assert result.get("stored") is True
        assert result.get("memory_id") == "mem_001"


class TestNotes:

    @pytest.mark.asyncio
    async def test_save_note(self, handlers, mock_store):
        result = await handlers.save_note(content="remember this")
        assert result.get("saved") is True
        assert "note_id" in result

    @pytest.mark.asyncio
    async def test_save_note_no_store(self):
        handlers = ToolHandlers(store=None)
        result = await handlers.save_note(content="x")
        assert "error" in result
