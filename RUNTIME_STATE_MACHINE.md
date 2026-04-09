# AdaOS: Runtime State Machine

## Principle

24/7 operation is a deployment property. States are a behavior property. You need both.

Ada runs continuously as a systemd daemon. But at any given moment, she is in exactly one top-level state that defines her behavior, resource usage, and interaction mode.

## Core States

```
IDLE                    — Running, available, not actively engaged. Wake word listening.
ATTENTIVE               — Recently engaged, lightly monitoring for follow-up.
LISTENING               — Capturing current user speech.
PROCESSING_FAST         — Fast local interpretation / acknowledgement / routing (Qwen 14B, <1s).
WAITING_BACKGROUND      — Background tasks running, no immediate user turn.
DISPATCHING             — Sending work to specialist agent or local subsystem.
VALIDATING              — Checking returned artifacts / results through validation gates.
SPEAKING                — Actively delivering spoken output via TTS.
AWAITING_USER_DECISION  — Blocked on Daniel's decision. Cannot proceed without input.
DEGRADED                — Some subsystem unavailable, operating in fallback mode.
ERROR                   — Unrecoverable issue requiring intervention or restart.
```

## State Definitions

### IDLE
- Wake word detector active
- Minimal resource usage
- No active conversation context loaded
- Background Executive tasks may still be running (IDLE refers to voice loop only)
- Transition in: 2min silence timeout from ATTENTIVE, or startup
- Transition out: wake word, button press, V2 scene detection

### ATTENTIVE
- Recently active, conversation context still loaded
- Lower threshold to engage (no wake word needed, just start talking)
- Decay timer running (default: 2 minutes → IDLE)
- Listening passively for follow-up
- Transition in: after SPEAKING completes, or after returning from IDLE on wake word
- Transition out: user speaks → LISTENING, timer expires → IDLE

### LISTENING
- User is speaking, audio being captured
- VAD (Voice Activity Detection) active
- Whisper STT processing on RTX 4070
- Transition in: user starts speaking from ATTENTIVE
- Transition out: end-of-utterance detected (VAD endpoint) → PROCESSING_FAST

### PROCESSING_FAST
- Ada Realtime running Qwen 14B on RTX 5060 Ti
- Intent classification (structured output, grammar-enforced)
- Quick response generation
- Must produce output within 1 second (acknowledgement rule)
- Transition in: from LISTENING (utterance complete)
- Transition out: response ready → SPEAKING, deep routing needed → SPEAKING (interim) + DISPATCHING, user decision needed → SPEAKING (question) + AWAITING_USER_DECISION

### WAITING_BACKGROUND
- No active user conversation turn
- Executive has tasks running in background (agent work, research, validation)
- Can coexist with IDLE or ATTENTIVE (voice loop state is separate from executive state)
- Transition in: task dispatched, no user interaction pending
- Transition out: agent result arrives → VALIDATING, user initiates conversation → LISTENING

### DISPATCHING
- Ada Executive sending structured task packet to agent
- OpenClaw (Arch/Forge) subprocess spawn
- Brief period, transitions quickly
- Transition in: from PROCESSING_FAST (dispatch decision) or from VALIDATING (retry)
- Transition out: dispatch confirmed → WAITING_BACKGROUND, dispatch failed → DEGRADED or retry

### VALIDATING
- Ada Executive running validation gates on returned result
- Gate 1 (structural) → Gate 2 (technical) → Gate 3 (semantic) → Gate 4 (attention-worthiness)
- Transition in: agent result arrives from WAITING_BACKGROUND
- Transition out: all gates pass → SPEAKING (deliver), gate fails → DISPATCHING (retry), max retries → SPEAKING (report failure), needs decision → AWAITING_USER_DECISION

### SPEAKING
- TTS output active (Kokoro on CPU, streaming)
- Sentence-by-sentence delivery as LLM tokens arrive
- Interruptible (barge-in → cancel TTS + LLM generation)
- Transition in: from PROCESSING_FAST, VALIDATING, or AWAITING_USER_DECISION (user responded)
- Transition out: speech complete → ATTENTIVE, barge-in → LISTENING

### AWAITING_USER_DECISION
- Blocked on Daniel's input
- Ada has presented options or escalated a decision
- Cannot proceed on this task until user responds
- Other background tasks may continue independently
- Transition in: from PROCESSING_FAST (strategic ambiguity), from VALIDATING (agent conflict, architecture fork)
- Transition out: user responds → PROCESSING_FAST or DISPATCHING, user says "skip" or "cancel" → task cancelled

### DEGRADED
- One or more subsystems unavailable
- Ada operates in reduced mode with clear communication about limitations
- Sub-states based on what's down:
  - Ollama down → "My brain is offline. I can still take notes." (note-taking only)
  - OpenClaw/Claude down → local-only mode, agent tasks queued in PostgreSQL for later dispatch
  - Whisper down → text-only mode (if available), or fully degraded
  - Internet down → local-only, no web search, Ada local work still functions
- Transition in: health check detects failure from ANY state
- Transition out: health check detects recovery → previous state or IDLE

### ERROR
- Unrecoverable failure
- Ada cannot operate meaningfully
- Logs error, attempts notification if possible
- Requires restart or manual intervention
- Transition in: cascading failures, critical resource unavailable
- Transition out: restart → IDLE

## Transition Diagram

```
                    wake word / button
            ┌──────────────────────────┐
            │                          ▼
          IDLE ◄──── timeout ──── ATTENTIVE ◄──── speech done ────┐
            │                      │                               │
            │                      │ user speaks                   │
            │                      ▼                               │
            │                   LISTENING                          │
            │                      │                               │
            │                      │ end of utterance              │
            │                      ▼                               │
            │               PROCESSING_FAST                   SPEAKING
            │                 │    │    │                       ▲  ▲
            │     quick answer│    │    │needs decision        │  │
            │                 │    │    ▼                      │  │
            │                 │    │  AWAITING_USER_DECISION───┘  │
            │                 │    │         │                     │
            │                 │    │  user responds               │
            │                 │    ▼                               │
            │                 │  DISPATCHING                      │
            │                 │    │                               │
            │                 │    │ dispatch confirmed            │
            │                 │    ▼                               │
            │                 │  WAITING_BACKGROUND                │
            │                 │    │                               │
            │                 │    │ result arrives                │
            │                 │    ▼                               │
            │                 │  VALIDATING ──── retry ──► DISPATCHING
            │                 │    │                               │
            │                 │    │ gates pass                    │
            │                 │    └───────────────────────────────┘
            │                 │
            │                 └────────────────────────────────────┘
            │
            │          health check failure
            ├─────────────────────────────────► DEGRADED
            │                                     │
            │          service restored            │
            │◄────────────────────────────────────┘
            │
            │          cascading failure
            └─────────────────────────────────► ERROR
```

## Barge-In Behavior

When user interrupts Ada while SPEAKING:
1. VAD detects speech onset
2. InterruptionFrame propagates upstream
3. TTS playback cancelled immediately
4. LLM generation cancelled (stop wasting tokens)
5. State: SPEAKING → LISTENING
6. New utterance processed from scratch

Ada-specific override: sometimes Ada should resist interruption.
- "Hold on, this is important" — Ada can briefly resist barge-in for critical information
- Decision: Ada Realtime judges based on priority of current output vs. likely intent of interruption
- Default: always allow barge-in. Resist only for Gate 4 "immediate delivery" class results.

## Concurrent State Layers

The voice loop state and executive state can be independent:

```
Voice layer:  IDLE / ATTENTIVE / LISTENING / PROCESSING_FAST / SPEAKING
Executive:    WAITING_BACKGROUND / DISPATCHING / VALIDATING

These are concurrent. Examples:
- Voice: IDLE + Executive: WAITING_BACKGROUND (you walked away, Arch is still working)
- Voice: LISTENING + Executive: VALIDATING (you're talking while Forge's code is being checked)
- Voice: SPEAKING + Executive: DISPATCHING (Ada tells you about one thing while dispatching another)
```

This means the state machine is really two layers:

**Voice FSM:** IDLE → ATTENTIVE → LISTENING → PROCESSING_FAST → SPEAKING → ATTENTIVE → ...
**Executive FSM:** IDLE → DISPATCHING → WAITING_BACKGROUND → VALIDATING → IDLE → ...

Plus overlay states that affect both: DEGRADED, ERROR, AWAITING_USER_DECISION.

## Timeouts and Decay

| Transition | Timeout | Configurable |
|-----------|---------|--------------|
| ATTENTIVE → IDLE | 2 minutes | Yes |
| PROCESSING_FAST → must speak | 1 second | Hard limit |
| WAITING_BACKGROUND → stale check | 1 hour | Yes |
| AWAITING_USER_DECISION → reminder | 10 minutes | Yes |
| DISPATCHING → timeout | 30 seconds | Yes |

## Health Check Loop

Runs every 60 seconds (configurable). Checks:

| Service | Check | Failure → |
|---------|-------|-----------|
| Ollama (GPT-OSS 20B) | HTTP ping + simple inference | DEGRADED (note-taking only) |
| PostgreSQL | Write test | ERROR (cannot persist) |
| OpenClaw / Claude API | HTTP ping | DEGRADED (local-only, queue agent tasks in PostgreSQL) |
| Whisper STT | Process alive + GPU memory | DEGRADED (text-only) |
| Kokoro TTS | Process alive | DEGRADED (text-only output) |
| Disk space | >1GB free | Warning → DEGRADED if critical |
| GPU memory | nvidia-smi | Warning if >90% utilization |

Recovery: when a failed check passes again, transition out of DEGRADED. Log recovery event.

## Implementation: state.py

```python
from enum import Enum, auto

class VoiceState(Enum):
    IDLE = auto()
    ATTENTIVE = auto()
    LISTENING = auto()
    PROCESSING_FAST = auto()
    SPEAKING = auto()

class ExecutiveState(Enum):
    IDLE = auto()
    DISPATCHING = auto()
    WAITING_BACKGROUND = auto()
    VALIDATING = auto()

class OverlayState(Enum):
    NONE = auto()
    AWAITING_USER_DECISION = auto()
    DEGRADED = auto()
    ERROR = auto()

# Legal transitions
VOICE_TRANSITIONS = {
    VoiceState.IDLE: [VoiceState.ATTENTIVE],
    VoiceState.ATTENTIVE: [VoiceState.LISTENING, VoiceState.IDLE],
    VoiceState.LISTENING: [VoiceState.PROCESSING_FAST],
    VoiceState.PROCESSING_FAST: [VoiceState.SPEAKING],
    VoiceState.SPEAKING: [VoiceState.ATTENTIVE, VoiceState.LISTENING],  # LISTENING = barge-in
}

EXECUTIVE_TRANSITIONS = {
    ExecutiveState.IDLE: [ExecutiveState.DISPATCHING],
    ExecutiveState.DISPATCHING: [ExecutiveState.WAITING_BACKGROUND, ExecutiveState.IDLE],
    ExecutiveState.WAITING_BACKGROUND: [ExecutiveState.VALIDATING, ExecutiveState.IDLE],
    ExecutiveState.VALIDATING: [ExecutiveState.DISPATCHING, ExecutiveState.IDLE],  # DISPATCHING = retry
}
```

## Execution Journal Integration

Every state transition is logged to the execution journal:

```json
{
  "timestamp": "2026-04-02T10:30:15Z",
  "layer": "voice",
  "from_state": "PROCESSING_FAST",
  "to_state": "SPEAKING",
  "trigger": "response_ready",
  "task_id": "tsk_20260402_001",
  "details": "Acknowledging brainstorm capture"
}
```

This provides a complete behavioral trace without requiring permissions. High autonomy + full observability.
