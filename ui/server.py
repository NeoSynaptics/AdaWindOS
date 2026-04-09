"""Ada UI server — serves the chat frontend and bridges WebSocket to Ada's engine.

Run standalone:
    python -m ui.server              # UI only (no Ada backend, echoes messages)
    python -m ui.server --with-ada   # Full Ada backend wired up

Or import and start programmatically from ada/main.py.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

log = logging.getLogger("ada.ui")

UI_DIR = Path(__file__).parent
HOST = "0.0.0.0"
PORT = 8765

app = FastAPI(title="Ada UI")


# ── Ada instance (set when running with --with-ada) ──

_ada_instance = None
_ada_config = None


def set_ada(ada_instance, config=None):
    """Wire the Ada engine into the UI server."""
    global _ada_instance, _ada_config
    _ada_instance = ada_instance
    _ada_config = config


# ── Serve the frontend ──

@app.get("/")
async def index():
    return FileResponse(UI_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "ada_connected": _ada_instance is not None}


# ── WebSocket chat endpoint ──

clients: set[WebSocket] = set()


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    log.info(f"Chat client connected ({len(clients)} total)")

    # Send model info on connect
    model_name = "—"
    if _ada_config:
        model_name = _ada_config.models.control_model
    await websocket.send_json({"type": "model", "name": model_name})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            message = data.get("message", "").strip()
            source = data.get("source", "text")
            if not message:
                continue

            log.info(f"[{source}] {message[:80]}")

            if _ada_instance:
                # Stream response from Ada
                await _handle_ada_message(websocket, message, source)
            else:
                # No Ada backend — echo mode for UI development
                await _handle_echo(websocket, message)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WebSocket error: {e}")
    finally:
        clients.discard(websocket)
        log.info(f"Chat client disconnected ({len(clients)} total)")


async def _handle_ada_message(ws: WebSocket, message: str, source: str):
    """Process a message through Ada's streaming pipeline."""
    collected_sentences = []

    async def push_sentence(sentence: str):
        """Called by Ada for each sentence of the response."""
        collected_sentences.append(sentence)
        if len(collected_sentences) == 1:
            # First sentence — start streaming
            await ws.send_json({"type": "stream_start"})
        await ws.send_json({"type": "stream_token", "token": sentence + " "})

    try:
        source_type = "user_voice" if source == "voice" else "user_text"
        response = await _ada_instance.process_input_streaming(
            message,
            push_sentence_fn=push_sentence,
            source_type=source_type,
        )

        if collected_sentences:
            # Streaming was used — send end
            full_text = " ".join(collected_sentences)
            await ws.send_json({"type": "stream_end", "content": full_text})
        elif response:
            # Non-streaming response
            await ws.send_json({"type": "response", "content": response})
        else:
            await ws.send_json({"type": "response", "content": "I didn't catch that."})

    except Exception as e:
        log.error(f"Ada processing error: {e}", exc_info=True)
        await ws.send_json({
            "type": "error",
            "message": "Something went wrong. Try again.",
        })


async def _handle_echo(ws: WebSocket, message: str):
    """Echo mode — for UI development without Ada backend."""
    await asyncio.sleep(0.3)
    await ws.send_json({"type": "stream_start"})

    # Simulate streaming response
    words = f"Echo: {message}".split()
    for i, word in enumerate(words):
        token = word + (" " if i < len(words) - 1 else "")
        await ws.send_json({"type": "stream_token", "token": token})
        await asyncio.sleep(0.05)

    full = " ".join(words)
    await ws.send_json({"type": "stream_end", "content": full})


# ── Broadcast to all clients (for voice pipeline integration) ──

async def broadcast_transcript(text: str, source: str = "voice"):
    """Push a voice transcript to all connected UI clients.

    Call this from the voice pipeline's AdaBridge when STT produces text,
    so the transcribed speech appears in the chat UI.
    """
    msg = json.dumps({"type": "voice_transcript", "text": text, "source": source})
    dead = set()
    for client in clients:
        try:
            await client.send_text(msg)
        except Exception:
            dead.add(client)
    clients -= dead


async def broadcast_ada_response(text: str):
    """Push an Ada response to all connected UI clients.

    Call this from the voice pipeline's AdaBridge so spoken responses
    also appear in the chat.
    """
    msg = json.dumps({"type": "response", "content": text})
    dead = set()
    for client in clients:
        try:
            await client.send_text(msg)
        except Exception:
            dead.add(client)
    clients -= dead


# ── Entry point ──

def start_server(ada_instance=None, config=None, host=HOST, port=PORT):
    """Start the UI server (blocking). Call from Ada's startup or standalone."""
    if ada_instance:
        set_ada(ada_instance, config)
    uvicorn.run(app, host=host, port=port, log_level="info")


async def start_server_async(ada_instance=None, config=None, host=HOST, port=PORT):
    """Start the UI server as an async task (non-blocking)."""
    if ada_instance:
        set_ada(ada_instance, config)
    server_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    with_ada = "--with-ada" in sys.argv

    if with_ada:
        # Full Ada mode
        async def run():
            # Add project root to path
            project_root = str(Path(__file__).parent.parent)
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            from ada.config import AdaConfig
            from ada.main import Ada

            ada = Ada()
            await ada.start()
            set_ada(ada, ada.config)

            log.info(f"Ada UI server starting on http://{HOST}:{PORT}")
            await start_server_async(ada, ada.config)

        asyncio.run(run())
    else:
        # UI-only mode (echo)
        log.info(f"Ada UI server starting on http://{HOST}:{PORT} (echo mode — no Ada backend)")
        start_server()
