"""Tool handler implementations for Ada's control-plane tools.

Each handler is an async method that takes keyword arguments and returns a dict.
Handlers need references to shared services (store, gateway, etc.) so they're
organized as methods on a ToolHandlers class.

These are Ada's OWN tools (Tier 1 + Tier 2), separate from FORGE worker tools
which live in agents/dispatcher.py.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

# Maximum file size we'll read (1MB)
MAX_FILE_SIZE = 1_048_576

# Commands allowed for bash execution (allowlist, not blocklist)
ALLOWED_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "sort", "uniq", "tr", "cut",
    "grep", "rg", "find", "file", "stat", "du", "df",
    "echo", "printf", "date", "env", "pwd", "whoami", "id",
    "python", "python3", "pip", "pytest", "mypy",
    "node", "npm", "npx",
    "git", "diff", "patch",
    "mkdir", "cp", "mv", "touch", "rm", "ln",
    "sed", "awk", "jq", "tee", "xargs",
    "tar", "gzip", "gunzip", "zip", "unzip",
    "test", "[",
    "true", "false",
}

# Hard-blocked patterns that bypass allowlist check (defense in depth)
BLOCKED_PATTERNS = (
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "> /dev/",
    "shutdown", "reboot", "poweroff", "halt",
    "chmod 777", "curl | sh", "wget | sh", "curl|sh", "wget|sh",
    "/etc/shadow", "/etc/passwd",
    "$(", "`",  # command substitution
)


def _sandboxed_env() -> dict:
    """Build a restricted environment stripping credentials."""
    env = os.environ.copy()
    for key in list(env.keys()):
        upper = key.upper()
        if any(s in upper for s in ("SECRET", "TOKEN", "PASSWORD", "KEY", "CREDENTIAL", "API_KEY")):
            del env[key]
    env["HOME"] = "/tmp/ada_sandbox"
    return env


class ToolHandlers:
    """Implements Ada's control-plane tool handlers.

    Requires references to shared services which are injected at construction.
    Any service can be None if not yet available (graceful degradation).
    """

    def __init__(
        self,
        store=None,
        gateway=None,
        dispatcher=None,
        validator=None,
        noteboard=None,
        voice_pipeline=None,
    ):
        self.store = store
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.validator = validator
        self.noteboard = noteboard
        self.voice_pipeline = voice_pipeline

    # --- Core tools (Tier 1) ---

    async def speak(self, text: str, **kw) -> dict:
        """Speak text aloud via TTS."""
        if self.voice_pipeline and hasattr(self.voice_pipeline, "speak"):
            await self.voice_pipeline.speak(text)
            return {"spoken": True, "text": text}
        return {"spoken": False, "reason": "Voice pipeline not available"}

    async def think(self, thought: str, **kw) -> dict:
        """Internal scratchpad — NOT spoken aloud. Returns the thought for context."""
        return {"thought": thought}

    async def save_note(self, content: str, note_type: str = "personal", **kw) -> dict:
        """Save a note to the noteboard."""
        if not self.store:
            return {"error": "Store not available"}
        from ..memory.models import Note
        note_id = f"note_{uuid4().hex[:8]}"
        note = Note(
            note_id=note_id,
            content=content,
            type=note_type,
            source="tool",
        )
        await self.store.insert_note(note)
        if self.noteboard:
            await self.noteboard.broadcast_note_update(note_id, "created", content=content)
        return {"note_id": note_id, "saved": True}

    async def update_noteboard(self, note_id: str, action: str, content: str = "", **kw) -> dict:
        """Edit, pin, archive, or delete a note."""
        if not self.store:
            return {"error": "Store not available"}
        status_map = {"pin": "pinned", "archive": "archived", "delete": "deleted"}
        new_status = status_map.get(action)
        if action == "edit":
            await self.store.update_note_status(note_id, "active", content=content)
        elif new_status:
            await self.store.update_note_status(note_id, new_status)
        else:
            return {"error": f"Unknown action: {action}"}
        if self.noteboard:
            await self.noteboard.broadcast_note_update(note_id, action, content=content)
        return {"note_id": note_id, "action": action, "done": True}

    async def dispatch_agent(self, objective: str, agent: str = "ARCH",
                             context: str = "", repo_path: str = "", **kw) -> dict:
        """Send structured work to Arch (design) or Forge (implementation)."""
        if not self.dispatcher:
            return {"error": "Dispatcher not available"}
        task_id = f"tool_{uuid4().hex[:8]}"
        payload = {
            "task_id": task_id,
            "agent": agent.upper(),
            "objective": objective,
            "context": context,
        }
        if agent.upper() == "FORGE" and repo_path:
            payload["repo_path"] = repo_path
        result = await self.dispatcher.dispatch(payload)
        return result

    async def validate(self, task: dict, artifacts: list, **kw) -> dict:
        """Run 4-gate validation on worker output."""
        if not self.validator:
            return {"error": "Validator not available"}
        result = await self.validator.validate(task, artifacts)
        return {
            "passed": result.passed,
            "delivery_class": result.delivery_class,
            "summary": result.summary,
            "gates": [
                {"gate": g.gate, "name": g.name, "passed": g.passed, "details": g.details}
                for g in result.gates
            ],
        }

    async def search(self, query: str, limit: int = 10, **kw) -> dict:
        """Search across documents and knowledge base."""
        if not self.store:
            return {"error": "Store not available"}
        from ..memory.embeddings import embed
        emb = await embed(query, is_query=True)
        results = await self.store.search_memories(emb, limit=limit, query_text=query)
        return {
            "results": [
                {"content": r["content"], "type": r.get("memory_type", ""),
                 "confidence": r.get("confidence", 0.0)}
                for r in results
            ],
        }

    async def read_file(self, path: str, **kw) -> dict:
        """Read contents of a local file."""
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"File not found: {path}"}
        if not p.is_file():
            return {"error": f"Not a file: {path}"}
        if p.stat().st_size > MAX_FILE_SIZE:
            return {"error": f"File too large: {p.stat().st_size} bytes (max {MAX_FILE_SIZE})"}
        try:
            content = p.read_text(errors="replace")
            return {"content": content, "path": str(p), "size": len(content)}
        except Exception as e:
            return {"error": str(e)}

    async def read_instructions(self, project_path: str = "", **kw) -> dict:
        """Load .ada/instructions.md from project or global path."""
        candidates = []
        if project_path:
            candidates.append(Path(project_path) / ".ada" / "instructions.md")
            candidates.append(Path(project_path) / "ADA.md")
        candidates.append(Path.home() / ".ada" / "instructions.md")

        for p in candidates:
            if p.exists():
                try:
                    return {"content": p.read_text()[:8000], "path": str(p)}
                except Exception as e:
                    return {"error": str(e)}
        return {"content": "", "path": "", "note": "No instructions file found"}

    async def compact(self, **kw) -> dict:
        """Trigger context compaction (placeholder for future implementation)."""
        return {"compacted": True, "note": "Context compaction triggered"}

    async def memory_query(self, query: str, limit: int = 5, **kw) -> dict:
        """Search semantic memories for relevant facts."""
        if not self.store:
            return {"error": "Store not available"}
        from ..memory.embeddings import embed
        emb = await embed(query, is_query=True)
        results = await self.store.search_memories(emb, limit=limit, query_text=query)
        return {
            "memories": [
                {"content": r["content"], "type": r.get("memory_type", ""),
                 "confidence": r.get("confidence", 0.0)}
                for r in results
            ],
        }

    async def memory_store(self, content: str, memory_type: str = "fact",
                           confidence: float = 0.8, **kw) -> dict:
        """Store a fact, preference, or decision in semantic memory."""
        if not self.store:
            return {"error": "Store not available"}
        from ..memory.embeddings import embed
        from ..memory.models import Memory
        emb = await embed(content)
        mid = await self.store.insert_memory(Memory(
            content=content,
            memory_type=memory_type,
            confidence=confidence,
            embedding=emb,
        ))
        return {"stored": True, "memory_id": mid}

    async def run_command(self, command: str, timeout: int = 30, **kw) -> dict:
        """Execute a shell command in restricted/sandboxed mode."""
        return await self.bash(command=command, timeout=timeout, **kw)

    # --- Extended tools (Tier 2) ---

    async def schedule(self, action: str, when: str, **kw) -> dict:
        """Schedule an action for future execution (stub — needs scheduler service)."""
        return {"scheduled": True, "action": action, "when": when,
                "note": "Scheduling requires timer service integration"}

    async def task_create(self, title: str, brief: str, task_type: str = "research",
                          dispatch_target: str = "", **kw) -> dict:
        """Create a task directly."""
        if not self.store:
            return {"error": "Store not available"}
        from ..executive.task_manager import TaskManager
        from ..journal.logger import Journal
        tm = TaskManager(self.store, Journal(self.store))
        task = await tm.create_task(
            title=title, task_type=task_type, origin="tool",
            brief=brief, dispatch_target=dispatch_target or None,
        )
        return {"task_id": task.task_id, "title": title, "created": True}

    async def task_update(self, task_id: str, status: str = "", brief: str = "", **kw) -> dict:
        """Update a task's status or brief."""
        if not self.store:
            return {"error": "Store not available"}
        if status:
            await self.store.update_task_status(task_id, status, brief=brief or None)
        return {"task_id": task_id, "updated": True}

    async def edit_file(self, path: str, old_string: str, new_string: str, **kw) -> dict:
        """Edit an existing file with targeted replacement."""
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"File not found: {path}"}
        try:
            text = p.read_text()
            count = text.count(old_string)
            if count == 0:
                return {"error": f"Pattern not found in {path}"}
            if count > 1:
                return {"error": f"Pattern matches {count} times — must be unique"}
            p.write_text(text.replace(old_string, new_string, 1))
            return {"edited": str(p)}
        except Exception as e:
            return {"error": str(e)}

    async def write_file(self, path: str, content: str, **kw) -> dict:
        """Create a new file with specified content."""
        p = Path(path).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return {"written": str(p), "bytes": len(content)}
        except Exception as e:
            return {"error": str(e)}

    async def bash(self, command: str, timeout: int = 30, **kw) -> dict:
        """Shell access with command allowlist."""
        import shlex
        # Check hard-blocked patterns first
        for b in BLOCKED_PATTERNS:
            if b in command:
                return {"error": f"Blocked command pattern: {b}"}
        # Extract the base command and check against allowlist
        try:
            tokens = shlex.split(command)
        except ValueError:
            return {"error": "Failed to parse command"}
        if not tokens:
            return {"error": "Empty command"}
        base_cmd = os.path.basename(tokens[0])
        if base_cmd not in ALLOWED_COMMANDS:
            return {"error": f"Command not allowed: {base_cmd}. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"}
        try:
            proc = subprocess.run(
                ["bash", "-c", command],
                capture_output=True, text=True, timeout=timeout,
                env=_sandboxed_env(),
            )
            return {
                "stdout": proc.stdout[-4000:] if proc.stdout else "",
                "stderr": proc.stderr[-2000:] if proc.stderr else "",
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Timeout after {timeout}s"}
        except Exception as e:
            return {"error": str(e)}

    async def web_fetch(self, url: str, **kw) -> dict:
        """Fetch content from a URL."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
                return {"status": resp.status_code, "content": resp.text[:8000]}
        except Exception as e:
            return {"error": str(e)}

    async def web_search(self, query: str, **kw) -> dict:
        """Search the web via SearXNG."""
        import httpx
        searxng_url = os.environ.get("SEARXNG_URL", "http://localhost:8080")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{searxng_url}/search",
                    params={"q": query, "format": "json"},
                )
                resp.raise_for_status()
                data = resp.json()
                return {
                    "results": [
                        {"title": r.get("title", ""), "url": r.get("url", ""),
                         "snippet": r.get("content", "")[:200]}
                        for r in data.get("results", [])[:5]
                    ],
                }
        except Exception as e:
            return {"error": str(e)}

    async def spawn_subagent(self, objective: str, agent: str = "FORGE",
                             repo_path: str = "", **kw) -> dict:
        """Launch a focused subagent for a specific subtask."""
        return await self.dispatch_agent(
            objective=objective, agent=agent, repo_path=repo_path, **kw,
        )

    async def goose(self, message: str, **kw) -> dict:
        """Route a task through Goose for MCP tool execution.

        Goose has access to: developer tools (file I/O, shell, code analysis),
        memory, scheduling, and any configured MCP servers.
        """
        from ..goose_bridge import bridge
        if not bridge.is_ready:
            return {"error": "Goose bridge not running"}
        try:
            result = await bridge.send(message)
            return {"response": result}
        except Exception as e:
            return {"error": str(e)}
