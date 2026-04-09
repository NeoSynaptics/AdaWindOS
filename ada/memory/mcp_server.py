"""MCP server — exposes Ada's memory to external AI agents.

Implements the Model Context Protocol (JSON-RPC 2.0 over stdio) so that
Claude Code, Cursor, ChatGPT plugins, or any MCP-compatible client can
query Ada's episodic memory, semantic memory, and knowledge graph.

Inspired by MemPalace's 19-tool MCP interface, but backed by Ada's
PostgreSQL + pgvector stack instead of ChromaDB.

Usage (stdio transport):
    python -m ada.memory.mcp_server

Tools exposed:
    search_memories      — semantic search across extracted facts
    get_recent_episodes  — retrieve conversation history by session
    get_active_tasks     — list tasks in progress
    get_entity_graph     — knowledge graph traversal
    store_memory         — allow external agents to contribute facts
    health               — check database connectivity
"""

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any

from ..config import AdaConfig, DatabaseConfig, MCPConfig
from .store import Store, StoreError
from .embeddings import embed
from .models import Memory

log = logging.getLogger(__name__)

# ── MCP Protocol Constants ──

SERVER_INFO = {
    "name": "ada-memory",
    "version": "1.0.0",
}

CAPABILITIES = {
    "tools": {"listChanged": False},
}

TOOLS = [
    {
        "name": "search_memories",
        "description": (
            "Semantic search across Ada's extracted facts and memories. "
            "Returns the most relevant memories ranked by vector similarity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10, max 50)",
                    "default": 10,
                },
                "memory_type": {
                    "type": "string",
                    "description": "Filter by type: fact, preference, decision, pattern, correction",
                    "enum": ["fact", "preference", "decision", "pattern", "correction"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_recent_episodes",
        "description": (
            "Retrieve recent conversation turns from a session. "
            "Episodes include speaker, content, timestamp, and turn type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to retrieve episodes from",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max episodes to return (default 20, max 100)",
                    "default": 20,
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_active_tasks",
        "description": (
            "List all tasks currently in progress, queued, or awaiting results. "
            "Includes task ID, title, status, owner, priority, and dispatch target."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_entity_graph",
        "description": (
            "Traverse Ada's knowledge graph starting from a named entity. "
            "Returns the entity and its relations up to the specified depth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "Name of the entity to look up",
                },
                "depth": {
                    "type": "integer",
                    "description": "Traversal depth (default 2, max 4)",
                    "default": 2,
                },
            },
            "required": ["entity_name"],
        },
    },
    {
        "name": "store_memory",
        "description": (
            "Store a new fact or observation in Ada's semantic memory. "
            "External agents can contribute knowledge this way."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact or observation to store",
                },
                "memory_type": {
                    "type": "string",
                    "description": "Type of memory",
                    "enum": ["fact", "preference", "decision", "pattern", "correction"],
                    "default": "fact",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score 0.0-1.0 (default 0.8)",
                    "default": 0.8,
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "health",
        "description": "Check Ada's memory system health and connectivity.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Tool Handlers ──

async def _handle_search_memories(store: Store, args: dict) -> list[dict]:
    query = args["query"]
    limit = min(args.get("limit", 10), 50)
    memory_type = args.get("memory_type")

    embedding = await embed(query, is_query=True)
    results = await store.search_memories(embedding, limit=limit, query_text=query)

    formatted = []
    for mem in results:
        if memory_type and mem.get("memory_type") != memory_type:
            continue
        formatted.append({
            "id": mem["id"],
            "content": mem["content"],
            "memory_type": mem["memory_type"],
            "confidence": float(mem["confidence"]),
            "distance": float(mem.get("distance", 0)),
            "valid_from": mem["valid_from"].isoformat() if mem.get("valid_from") else None,
        })
    return formatted


async def _handle_get_recent_episodes(store: Store, args: dict) -> list[dict]:
    session_id = args["session_id"]
    limit = min(args.get("limit", 20), 100)
    episodes = await store.get_recent_episodes(session_id, limit=limit)
    return [
        {
            "speaker": ep["speaker"],
            "content": ep["content"],
            "turn_type": ep["turn_type"],
            "timestamp": ep["timestamp"].isoformat() if ep.get("timestamp") else None,
        }
        for ep in episodes
    ]


async def _handle_get_active_tasks(store: Store, _args: dict) -> list[dict]:
    tasks = await store.get_active_tasks()
    return [
        {
            "task_id": t["task_id"],
            "title": t["title"],
            "status": t["status"],
            "owner": t["owner"],
            "priority": t["priority"],
            "type": t["type"],
            "dispatch_target": t.get("dispatch_target"),
        }
        for t in tasks
    ]


async def _handle_get_entity_graph(store: Store, args: dict) -> dict:
    name = args["entity_name"]
    depth = min(args.get("depth", 2), 4)

    # Look up entity by name
    row = await store.pool.fetchrow(
        "SELECT * FROM entities WHERE name ILIKE $1", name,
    )
    if not row:
        return {"error": f"Entity '{name}' not found"}

    entity = dict(row)
    relations = await store.get_entity_relations(entity["id"], depth=depth)
    return {
        "entity": {
            "id": entity["id"],
            "name": entity["name"],
            "entity_type": entity["entity_type"],
        },
        "relations": [
            {
                "predicate": r["predicate"],
                "object_name": r["object_name"],
                "object_type": r["object_type"],
                "confidence": float(r["confidence"]),
            }
            for r in relations
        ],
    }


async def _handle_store_memory(store: Store, args: dict) -> dict:
    content = args["content"]
    memory_type = args.get("memory_type", "fact")
    confidence = args.get("confidence", 0.8)

    embedding = await embed(content)
    mem = Memory(
        content=content,
        memory_type=memory_type,
        confidence=confidence,
        source_episode_ids=[],
        embedding=embedding,
    )
    mem_id = await store.insert_memory(mem)
    return {"id": mem_id, "status": "stored"}


async def _handle_health(store: Store, _args: dict) -> dict:
    healthy = await store.health_check()
    return {
        "status": "ok" if healthy else "degraded",
        "database": "connected" if healthy else "unreachable",
        "server": SERVER_INFO["name"],
        "version": SERVER_INFO["version"],
    }


TOOL_HANDLERS = {
    "search_memories": _handle_search_memories,
    "get_recent_episodes": _handle_get_recent_episodes,
    "get_active_tasks": _handle_get_active_tasks,
    "get_entity_graph": _handle_get_entity_graph,
    "store_memory": _handle_store_memory,
    "health": _handle_health,
}


# ── JSON-RPC Transport (stdio) ──

def _json_rpc_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _json_rpc_error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


async def _handle_request(store: Store, request: dict) -> dict | None:
    """Route a single JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    # MCP lifecycle
    if method == "initialize":
        return _json_rpc_response(req_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
        })

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "ping":
        return _json_rpc_response(req_id, {})

    # Tool listing
    if method == "tools/list":
        return _json_rpc_response(req_id, {"tools": TOOLS})

    # Tool execution
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return _json_rpc_error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result = await handler(store, tool_args)
            return _json_rpc_response(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            })
        except StoreError as e:
            return _json_rpc_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True,
            })
        except Exception as e:
            log.exception(f"Tool {tool_name} failed")
            return _json_rpc_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True,
            })

    return _json_rpc_error(req_id, -32601, f"Unknown method: {method}")


async def run_stdio(config: AdaConfig | None = None):
    """Run the MCP server on stdin/stdout (line-delimited JSON-RPC)."""
    config = config or AdaConfig()
    store = Store(config.database)
    await store.connect()
    log.info("Ada MCP memory server started (stdio transport)")

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    try:
        while True:
            line = await reader.readline()
            if not line:
                break

            line = line.decode("utf-8").strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                err = _json_rpc_error(None, -32700, "Parse error")
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
                continue

            response = await _handle_request(store, request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
    finally:
        await store.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,  # logs to stderr, protocol on stdout
    )
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
