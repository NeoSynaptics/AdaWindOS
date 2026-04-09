"""
Goose Bridge — connects Ada to goosed for MCP tool execution.

Ada keeps: voice pipeline, memory (pgvector), classification, APU management.
Goose provides: MCP tool ecosystem (developer, filesystem, code execution, etc.)

Architecture:
    Ada (voice + brain) → goose_bridge → goosed (HTTP/SSE) → MCP tools
"""

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)

GOOSED_BIN = os.environ.get(
    "GOOSED_BIN",
    os.path.expanduser("~/GitHub/goose/target/release/goosed"),
)
GOOSED_HOST = os.environ.get("GOOSED_HOST", "127.0.0.1")
GOOSED_PORT = int(os.environ.get("GOOSED_PORT", "3199"))
GOOSED_URL = f"http://{GOOSED_HOST}:{GOOSED_PORT}"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Secret key for goosed auth — generate once, reuse
_SECRET = os.environ.get("GOOSE_SERVER__SECRET_KEY", "ada_goose_bridge")


@dataclass
class GooseResponse:
    """A streamed chunk from goosed."""
    text: str = ""
    tool_call: dict | None = None
    tool_result: dict | None = None
    done: bool = False
    tokens: dict = field(default_factory=dict)


class GooseBridge:
    """Manages the goosed sidecar process and proxies tool calls."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._session_id: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._ready = False

    # ── Lifecycle ──

    async def start(self, working_dir: str = "~") -> None:
        """Start goosed sidecar and create a session."""
        working_dir = os.path.expanduser(working_dir)

        # Start goosed if not already running
        if not await self._is_running():
            self._start_process()
            # Wait for it to be ready
            for _ in range(30):
                if await self._is_running():
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("goosed failed to start within 30s")

        self._client = httpx.AsyncClient(
            base_url=GOOSED_URL,
            headers={"X-Secret-Key": _SECRET},
            timeout=httpx.Timeout(30.0, read=300.0),
        )

        # Create a session
        resp = await self._client.post(
            "/agent/start",
            json={"working_dir": working_dir},
        )
        resp.raise_for_status()
        data = resp.json()
        self._session_id = data.get("id") or data.get("session_id")
        self._ready = True
        log.info(f"Goose session started: {self._session_id}")

    def _start_process(self) -> None:
        """Launch goosed as a subprocess."""
        if not os.path.isfile(GOOSED_BIN):
            raise FileNotFoundError(
                f"goosed binary not found at {GOOSED_BIN}. "
                "Build it with: cd ~/GitHub/goose && cargo build --release -p goose-server"
            )

        env = os.environ.copy()
        env.update({
            "GOOSE_HOST": GOOSED_HOST,
            "GOOSE_PORT": str(GOOSED_PORT),
            "GOOSE_TLS": "false",
            "GOOSE_SERVER__SECRET_KEY": _SECRET,
            "OLLAMA_HOST": OLLAMA_HOST,
            "OLLAMA_TIMEOUT": "600",
        })

        self._process = subprocess.Popen(
            [GOOSED_BIN, "agent"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        log.info(f"Started goosed (pid={self._process.pid}) on {GOOSED_URL}")

    async def _is_running(self) -> bool:
        """Check if goosed is reachable."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(
                    f"{GOOSED_URL}/health",
                    headers={"X-Secret-Key": _SECRET},
                )
                return r.status_code == 200
        except Exception:
            return False

    async def stop(self) -> None:
        """Shutdown goosed."""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._ready = False
        self._session_id = None
        log.info("Goose bridge stopped")

    # ── Tool discovery ──

    async def get_tools(self) -> list[dict]:
        """Get all available tools from goosed."""
        self._ensure_ready()
        resp = await self._client.post(
            "/agent/get_tools",
            params={"session_id": self._session_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def add_extension(self, name: str, ext_type: str = "builtin", **kwargs) -> None:
        """Add an MCP extension at runtime."""
        self._ensure_ready()
        config = {"name": name, "type": ext_type, **kwargs}
        resp = await self._client.post(
            "/agent/add_extension",
            json={"session_id": self._session_id, "config": config},
        )
        resp.raise_for_status()
        log.info(f"Added extension: {name}")

    # ── Chat / tool execution ──

    async def send(self, message: str) -> str:
        """Send a message and collect the full response (non-streaming)."""
        chunks = []
        async for chunk in self.stream(message):
            if chunk.text:
                chunks.append(chunk.text)
        return "".join(chunks)

    async def stream(self, message: str) -> AsyncIterator[GooseResponse]:
        """Send a message and stream back responses."""
        self._ensure_ready()

        request_id = str(uuid.uuid4())

        # Send the message
        resp = await self._client.post(
            f"/sessions/{self._session_id}/reply",
            json={
                "request_id": request_id,
                "user_message": {
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                    "timestamp": int(time.time()),
                },
            },
        )
        resp.raise_for_status()

        # Stream events via SSE
        async with httpx.AsyncClient(
            base_url=GOOSED_URL,
            headers={"X-Secret-Key": _SECRET},
            timeout=httpx.Timeout(300.0),
        ) as sse_client:
            async with sse_client.stream(
                "GET",
                f"/sessions/{self._session_id}/events",
            ) as event_stream:
                buffer = ""
                async for line in event_stream.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if not data_str.strip():
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Only process events for our request
                        rid = data.get("chat_request_id") or data.get("request_id", "")
                        if rid and rid != request_id:
                            continue

                        chunk = self._parse_event(data)
                        if chunk:
                            yield chunk
                            if chunk.done:
                                return

    def _parse_event(self, data: dict) -> GooseResponse | None:
        """Parse an SSE event into a GooseResponse."""
        event_type = data.get("type", "")

        if event_type == "Message":
            msg = data.get("message", {})
            content = msg.get("content", [])
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)

            return GooseResponse(
                text="\n".join(text_parts) if text_parts else "",
                tokens=data.get("token_state", {}),
                done=True,
            )

        if event_type == "ToolRequest":
            return GooseResponse(
                tool_call=data.get("tool_call", data),
            )

        if event_type == "ToolResponse":
            return GooseResponse(
                tool_result=data.get("result", data),
            )

        # Streaming text chunks
        if "content" in data:
            return GooseResponse(text=data["content"])

        return None

    def _ensure_ready(self):
        if not self._ready or not self._session_id:
            raise RuntimeError("GooseBridge not started. Call start() first.")

    @property
    def is_ready(self) -> bool:
        return self._ready


# ── Singleton instance ──
bridge = GooseBridge()
