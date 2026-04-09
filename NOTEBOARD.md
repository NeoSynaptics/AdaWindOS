# Noteboard

The Noteboard is Ada's live UI — a bidirectional WebSocket canvas that displays notes, tasks, and real-time activity. Personal notes are never visible to workers; only Ada and the user can see them.

## Architecture

```
Ada Subsystems ──→ NoteboardServer ←──WebSocket──→ Browser UI (noteboard.html)
                        ↕
                    PostgreSQL
```

The server pushes state changes to all connected clients and receives edits back from the UI.

## WebSocket Protocol

### Server → Client Messages

#### full_state

Sent on initial connection. Contains all active notes and tasks.

```json
{
    "type": "full_state",
    "notes": [...],
    "tasks": [...],
    "timestamp": "2026-04-07T10:30:00"
}
```

#### note.created

A new note was captured (usually from voice).

```json
{
    "type": "note.created",
    "note_id": "note_abc123",
    "content": "Look into sensor fusion latency",
    "note_type": "actionable",
    "timestamp": "..."
}
```

#### note.update

A note's status changed.

```json
{
    "type": "note.update",
    "note_id": "note_abc123",
    "action": "pinned",  // pinned | archived | edited | candidate_task | promoted_to_task
    "timestamp": "..."
}
```

#### task.update

A task's status changed.

```json
{
    "type": "task.update",
    "task_id": "task_xyz",
    "status": "dispatched",  // draft | queued | dispatched | validating | done | failed
    "title": "Implement sensor fusion",
    "timestamp": "..."
}
```

#### live.text

Streaming text during live conversation (e.g., bullet points forming as Ada speaks).

```json
{
    "type": "live.text",
    "text": "- Sensor fusion approach decided: Kalman filter",
    "timestamp": "..."
}
```

#### health.update

Subsystem health status change.

```json
{
    "type": "health.update",
    "subsystem": "ollama",
    "status": "healthy",
    "details": "Model qwen3:14b loaded on GPU_0",
    "timestamp": "..."
}
```

#### error

Error response to a UI action.

```json
{
    "type": "error",
    "message": "Action failed: pin_note"
}
```

### Client → Server Messages

#### pin_note

```json
{"action": "pin_note", "note_id": "note_abc123"}
```

#### archive_note

```json
{"action": "archive_note", "note_id": "note_abc123"}
```

#### edit_note

```json
{"action": "edit_note", "note_id": "note_abc123", "content": "Updated content", "status": "captured"}
```

#### promote_note

Promotes a note to `candidate_task` status (first step toward becoming a task).

```json
{"action": "promote_note", "note_id": "note_abc123"}
```

## Note Lifecycle

```
captured → pinned → candidate_task → promoted_to_task → archived
                                 ↓
                             archived (if not promoted)
```

| Status | Meaning |
|--------|---------|
| `captured` | Just recorded from voice or text |
| `pinned` | User pinned it for attention |
| `candidate_task` | Flagged as potential work item |
| `promoted_to_task` | Converted to an Ada Executive task |
| `archived` | No longer active |

Notes have types: `actionable` (potential work) or `personal` (just a thought/reminder).

## Server

`ada/noteboard/server.py` — WebSocket server using the `websockets` library.

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Port | `8765` |
| URL | `ws://localhost:8765` |

Features:
- Client tracking (set of connected WebSocket connections)
- Full state sync on connect
- Broadcast to all clients
- UI message handling with error responses
- Graceful disconnect handling

### Broadcast API

Other Ada subsystems push updates through broadcast methods:

```python
await noteboard.broadcast_note_created(note_id, content, note_type)
await noteboard.broadcast_note_update(note_id, action)
await noteboard.broadcast_task_update(task_id, status, title)
await noteboard.broadcast_live_text(text)
await noteboard.broadcast_health(subsystem, status, details)
```

## Frontend

`ada/noteboard/noteboard.html` — vanilla JS + HTML/CSS. Currently a bare template.

**Not yet implemented:**
- Note cards with pin/archive/promote actions
- Task cards with status indicators
- Live text streaming display
- Health status panel
- WebSocket connection management with auto-reconnect

## Integration

- **Voice pipeline:** When Ada captures a note from voice, it's stored in PostgreSQL and broadcast to the noteboard.
- **Task manager:** Task lifecycle changes (created, dispatched, validating, done) are broadcast.
- **Outbox:** The `noteboard.push` outbox event type delivers updates through the outbox worker for restart-safe delivery.
- **UltraPlan:** Completed overnight plans can be pushed to the noteboard (`push_to_noteboard` config flag).

## Implementation Status

- WebSocket server: complete
- Full state sync on connect: complete
- Broadcast methods: complete
- UI message handling (pin, archive, edit, promote): complete
- Frontend (HTML/JS): **not implemented** (bare template)
- Auto-reconnect: not implemented
- Live text streaming UI: not implemented
- Health panel: not implemented
