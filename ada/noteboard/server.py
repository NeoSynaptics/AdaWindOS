"""Noteboard WebSocket server — bidirectional live canvas.

Pushes note/task updates to the UI. Receives edits from the UI.
Personal notes never visible to workers.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from ..memory.store import Store

log = logging.getLogger(__name__)


class NoteboardServer:
    def __init__(self, store: Store, host: str = "localhost", port: int = 8765):
        self.store = store
        self.host = host
        self.port = port
        self.clients: set[WebSocketServerProtocol] = set()
        self._server = None

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, self.host, self.port)
        log.info(f"Noteboard server running on ws://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Noteboard server stopped")

    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        self.clients.add(ws)
        log.info(f"Noteboard client connected ({len(self.clients)} total)")

        try:
            # Send current state on connect
            await self._send_full_state(ws)

            # Listen for edits from UI
            async for message in ws:
                await self._handle_ui_message(ws, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            log.info(f"Noteboard client disconnected ({len(self.clients)} total)")

    async def _send_full_state(self, ws: WebSocketServerProtocol) -> None:
        """Send current notes + tasks to a newly connected client."""
        notes = await self.store.get_active_notes()
        tasks = await self.store.get_active_tasks()

        await ws.send(json.dumps({
            "type": "full_state",
            "notes": notes,
            "tasks": tasks,
            "timestamp": datetime.now().isoformat(),
        }, default=str))

    async def _handle_ui_message(self, ws: WebSocketServerProtocol, raw: str) -> None:
        """Handle an edit from the noteboard UI."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON from noteboard UI: {raw[:100]}")
            return

        action = msg.get("action")
        note_id = msg.get("note_id")

        if not action:
            log.warning(f"Noteboard message missing 'action': {raw[:100]}")
            return

        try:
            if action == "pin_note":
                await self.store.update_note_status(note_id, "pinned")
                await self.broadcast_note_update(note_id, "pinned")

            elif action == "archive_note":
                await self.store.update_note_status(note_id, "archived")
                await self.broadcast_note_update(note_id, "archived")

            elif action == "edit_note":
                await self.store.update_note_status(
                    note_id, msg.get("status", "captured"),
                    content=msg.get("content"),
            )
                await self.broadcast_note_update(note_id, "edited")

            elif action == "promote_note":
                await self.store.update_note_status(note_id, "candidate_task")
                await self.broadcast_note_update(note_id, "candidate_task")

            else:
                log.warning(f"Unknown noteboard action: {action}")

        except Exception as e:
            log.error(f"Failed to handle noteboard action '{action}' for note {note_id}: {e}", exc_info=True)
            try:
                await ws.send(json.dumps({"type": "error", "message": f"Action failed: {action}"}))
            except Exception:
                pass

    # --- Broadcast methods (called by Ada subsystems) ---

    async def broadcast(self, message: dict) -> None:
        """Broadcast a message to all connected clients."""
        if not self.clients:
            return
        payload = json.dumps(message, default=str)
        await asyncio.gather(
            *[client.send(payload) for client in self.clients],
            return_exceptions=True,
        )

    async def broadcast_note_update(self, note_id: str, action: str, content: str | None = None) -> None:
        """Broadcast note update. Always includes content for edits so UI stays in sync."""
        payload = {
            "type": "note.update",
            "note_id": note_id,
            "action": action,
            "timestamp": datetime.now().isoformat(),
        }
        # For edit actions, fetch current content from DB so UI gets the update
        if content is not None:
            payload["content"] = content
        elif action == "edited":
            try:
                notes = await self.store.get_active_notes()
                for n in notes:
                    if n.get("note_id") == note_id:
                        payload["content"] = n.get("content", "")
                        break
            except Exception as e:
                log.warning(f"Could not fetch note content for broadcast: {e}")
        await self.broadcast(payload)

    async def broadcast_note_created(self, note_id: str, content: str, note_type: str) -> None:
        await self.broadcast({
            "type": "note.created",
            "note_id": note_id,
            "content": content,
            "note_type": note_type,
            "timestamp": datetime.now().isoformat(),
        })

    async def broadcast_task_update(self, task_id: str, status: str, title: str = "") -> None:
        await self.broadcast({
            "type": "task.update",
            "task_id": task_id,
            "status": status,
            "title": title,
            "timestamp": datetime.now().isoformat(),
        })

    async def broadcast_live_text(self, text: str) -> None:
        """Stream live text during conversation (bullet points forming)."""
        await self.broadcast({
            "type": "live.text",
            "text": text,
            "timestamp": datetime.now().isoformat(),
        })

    async def broadcast_health(self, subsystem: str, status: str, details: str = "") -> None:
        await self.broadcast({
            "type": "health.update",
            "subsystem": subsystem,
            "status": status,
            "details": details,
            "timestamp": datetime.now().isoformat(),
        })
