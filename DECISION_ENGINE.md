# AdaOS: Decision Engine

## The Atom

The atomic unit of AdaOS is not a Frame, not a Turn, not a Task. It is a **Decision Event**.

Everything else is either plumbing (transport, streaming, audio) or consequence (tasks, notes, journal entries, spoken words). The Decision Event is where Ada thinks, decides, and acts. It is the only place where intelligence happens.

Every input to Ada — a voice utterance, an agent result, a timer firing, a system event — produces exactly one Decision Event. The Decision Event captures: what came in, what Ada understood, what Ada decided to do, and what happened as a result.

## Why This Matters

If you start from Pipecat (Layer 1), you'll optimize latency and streaming but under-design decision correctness, task lifecycle, and failure handling. Those are the hard parts.

If you start from the Decision Event (Layer 3), everything else follows:
- The prompt output format is defined by the Decision Event schema
- The tool interface is defined by the action types
- The logging is defined by the side effects
- The debugging is defined by the replay capability
- The state machine transitions are triggered by decisions, not by frames

## Layer Architecture

```
Layer 1 — Transport (Pipecat)
  Frames, audio/text streaming, VAD, interruptions
  → dumb pipe, solved problem, don't design here

Layer 2 — Interaction
  Turns, partial transcripts, voice timing, endpointing
  → converts raw audio into complete utterances
  → still mechanical, no intelligence

Layer 3 — Decision Engine  ← THIS IS ADA
  Decision Events, intent classification, action selection, routing
  → every input produces a Decision Event
  → this is where all intelligence lives
  → this is what we design first

Layer 4 — Execution
  Task lifecycle, agent dispatch, tool calls, validation
  → mechanical execution of decisions
  → controlled by Action Resolver (code, not LLM)

Layer 5 — Persistence
  Notes, tasks, journal, memory, context
  → storage of decisions and their consequences
  → queryable history
```

## Decision Event Schema

```json
{
  "event_id": "evt_20260402_00182",
  "timestamp": "2026-04-02T10:30:15.432Z",
  "sequence_num": 182,

  "source": {
    "type": "user_voice | agent_result | timer | system | noteboard_ui | startup",
    "raw_input": "I've been thinking about adding real-time analytics to SportWave",
    "context_refs": ["session_current", "task_tsk_001"],
    "turn_id": "turn_042"
  },

  "classification": {
    "intent": "brainstorm | note | dispatch | question | decision | command | followup | greeting | farewell | acknowledgement | clarification | noop",
    "confidence": 0.92,
    "reasoning_trace": "User is introducing a new idea, exploratory tone, no specific action requested yet"
  },

  "decision": {
    "action": "create_note | update_note | promote_note | create_task | update_task | dispatch_agent | respond_only | ask_clarification | escalate_to_user | save_silently | noop",
    "target": "ARCH | FORGE | null",
    "band": 1,
    "requires_confirmation": false,
    "confidence": 0.88
  },

  "execution": {
    "tool_calls": [
      {
        "tool": "save_note",
        "params": {"content": "real-time analytics for SportWave", "type": "actionable", "status": "captured"},
        "result": {"note_id": "note_0044", "success": true}
      },
      {
        "tool": "speak",
        "params": {"text": "Interesting. Let me capture that."},
        "result": {"success": true, "duration_ms": 1200}
      }
    ],
    "state_transitions": [
      {"layer": "voice", "from": "PROCESSING_FAST", "to": "SPEAKING"},
      {"layer": "executive", "from": "IDLE", "to": "IDLE"}
    ],
    "side_effects": [
      "note_created:note_0044",
      "noteboard_updated",
      "spoken_response"
    ]
  },

  "budget": {
    "local_tokens_used": 342,
    "cloud_tokens_used": 0,
    "cumulative_cloud_today": 0
  },

  "meta": {
    "processing_time_ms": 287,
    "model_used": "qwen3:14b",
    "latency_to_first_audio_ms": 412
  }
}
```

### Field-by-field justification

**source** — Where the input came from. Not just the text, but the context it arrived in. `context_refs` links to active tasks, recent notes, current session — this is what the LLM sees when making the decision.

**classification** — What Ada thinks the user means. This is the LLM's structured output. `reasoning_trace` is optional but valuable for debugging — why did Ada classify this way?

**decision** — What Ada decides to do about it. This is the critical part. `action` is the chosen action type. `band` maps to the policy band (1=free, 2=logged, 3=escalate). `requires_confirmation` is set by the Action Resolver, not the LLM.

**execution** — What actually happened. Tool calls with params and results. State transitions triggered. Side effects produced. This is the ground truth.

**budget** — Token accounting per event. Running totals.

**meta** — Performance data. Processing time, model used, latency.

## Intent Taxonomy (exhaustive, closed set)

Ada must classify every input into exactly one intent. No free-form. This is enforced via constrained generation grammar.

| Intent | Description | Typical source | Example |
|--------|-------------|---------------|---------|
| `brainstorm` | Exploring an idea, no specific action yet | user_voice | "I've been thinking about sensor fusion" |
| `note` | Explicitly saving something for later | user_voice | "Just save this — check Qwen 3.5 docs" |
| `dispatch` | Explicitly requesting agent work | user_voice | "Send this to Arch" / "Have Forge prototype this" |
| `question` | Asking for information or analysis | user_voice | "What do you think about this?" / "Is this feasible?" |
| `decision` | Making a choice between presented options | user_voice | "Go with streaming pipeline" / "Yes, that one" |
| `command` | Direct operational instruction | user_voice | "Remind me Friday" / "Cancel that task" / "Compact" |
| `followup` | Continuing/expanding a previous topic | user_voice | "Also we'd need 60fps minimum" |
| `greeting` | Starting a session | user_voice | "Hey Ada" / "Good morning" |
| `farewell` | Ending a session | user_voice | "That's it for now" / "Thanks" |
| `acknowledgement` | Confirming receipt, no action | user_voice | "OK" / "Got it" / "Sure" |
| `clarification` | Responding to Ada's question | user_voice | "I meant the camera, not the radar" |
| `noop` | Input that requires no action | system | Background noise classified as speech, empty transcript |
| `agent_delivery` | Agent returning a result | agent_result | Arch submits spec document |
| `timer_trigger` | Scheduled event firing | timer | "Call supplier" reminder at Friday 9am |
| `system_event` | Health check, startup, error | system | Ollama goes down, Ada startup |

### Classification rules

**Single intent per event.** A complex utterance like "Save this and send it to Arch" gets the higher-commitment intent (`dispatch`), with the lower one (`note`) handled as a side effect.

**Confidence threshold:** If confidence < 0.6, Ada asks for clarification instead of acting. If confidence 0.6-0.8, Ada acts but includes uncertainty in her response ("I think you want me to...").

**Fallback chain:** Grammar parse succeeds → use classification. Parse fails → retry with simplified prompt (1 try). Still fails → keyword matching (contains "arch" → dispatch, contains "save" → note, etc.). Still ambiguous → ask user.

## Action Taxonomy (exhaustive, closed set)

Given a classified intent, the Action Resolver selects an action. This mapping is **code, not LLM.** The LLM classifies. Code decides.

| Action | Description | Band | Triggers |
|--------|-------------|------|----------|
| `create_note` | Save a new note to noteboard | 1 | brainstorm, note, followup (new topic) |
| `update_note` | Append to or modify existing note | 1 | followup (same topic) |
| `promote_note` | Move note from captured → candidate_task or promoted_to_task | 1 | dispatch intent on existing note, explicit "make this a task" |
| `create_task` | Create a new Task object | 2 | dispatch (new work), promote_note result |
| `update_task` | Modify existing task (status, brief, artifacts) | 2 | decision (user chose), agent_delivery (result arrived) |
| `dispatch_agent` | Send structured task packet to Arch/Forge via OpenClaw | 2 | dispatch intent + task exists + brief structured + budget allows |
| `respond_only` | Speak a response, no other action | 1 | question (answerable locally), greeting, farewell, acknowledgement |
| `ask_clarification` | Ada asks user to clarify | 1 | Low confidence, ambiguous intent, strategic ambiguity |
| `escalate_to_user` | Present decision/options to user | 1 | Architecture fork, agent conflict, budget threshold |
| `save_silently` | Store information without speaking | 1 | noop with useful metadata, background context |
| `execute_command` | Run a direct command (schedule, cancel, compact) | 1-2 | command intent |
| `validate_result` | Run 4-gate validation on agent output | 2 | agent_delivery |
| `retry_dispatch` | Resend to agent with feedback | 2 | validate_result failed, retry_count < 3 |
| `deliver_result` | Surface validated result to user | 1 | validate_result passed gate 4 |
| `queue_ultraplan` | Queue for overnight planning | 2 | Complex brainstorm that needs deep planning |
| `noop` | Do nothing | 1 | Background noise, empty input |

## The Action Resolver (code, not LLM)

The Action Resolver is a deterministic function. It takes a Decision Event (with classification filled in) and determines the action. The LLM does NOT choose the action. The LLM classifies intent. Code resolves action.

This is the most important architectural decision in the system.

### Why code, not LLM?

- **Reliability:** A 14B model will sometimes pick the wrong action. Code never picks the wrong action for a given intent.
- **Safety:** Budget caps, band enforcement, retry limits — these must be deterministic.
- **Debuggability:** If Ada does something wrong, you can trace: was the intent classification wrong (LLM bug) or was the action resolution wrong (code bug)? Clear separation.
- **Speed:** Action resolution is instant. No LLM call needed.

### Resolution Rules

```python
def resolve_action(event: DecisionEvent, state: SystemState) -> Action:
    intent = event.classification.intent
    confidence = event.classification.confidence

    # Rule 0: Low confidence → ask
    if confidence < 0.6 and intent not in ("greeting", "farewell", "noop"):
        return Action(action="ask_clarification", band=1)

    # Rule 1: Greetings/farewells → respond only
    if intent in ("greeting", "farewell", "acknowledgement"):
        return Action(action="respond_only", band=1)

    # Rule 2: Noop → noop
    if intent == "noop":
        return Action(action="noop", band=1)

    # Rule 3: Agent delivery → validate
    if intent == "agent_delivery":
        return Action(action="validate_result", band=2)

    # Rule 4: Timer trigger → execute command
    if intent == "timer_trigger":
        return Action(action="execute_command", band=1)

    # Rule 5: System event → handle internally
    if intent == "system_event":
        return resolve_system_event(event, state)

    # Rule 6: Explicit note → create note
    if intent == "note":
        return Action(action="create_note", band=1)

    # Rule 7: Brainstorm → create note (might promote later)
    if intent == "brainstorm":
        if is_complex_enough_for_ultraplan(event):
            return Action(action="queue_ultraplan", band=2)
        return Action(action="create_note", band=1)

    # Rule 8: Followup → update existing note or create new
    if intent == "followup":
        if state.has_active_note_on_topic(event):
            return Action(action="update_note", band=1)
        return Action(action="create_note", band=1)

    # Rule 9: Question → check if needs agent
    if intent == "question":
        if needs_specialist(event):
            # Check if task exists for this topic
            if state.has_task_for_topic(event):
                return Action(action="dispatch_agent", target=route_to_agent(event), band=2)
            else:
                # Create task first, then dispatch
                return Action(action="create_task", band=2, then="dispatch_agent")
        return Action(action="respond_only", band=1)

    # Rule 10: Dispatch → promote note if exists, create task, dispatch
    if intent == "dispatch":
        target = route_to_agent(event)
        if not budget_allows(state, target):
            return Action(action="escalate_to_user", band=1,
                          reason="budget_threshold")
        if state.has_note_for_topic(event):
            return Action(action="promote_note", band=1, then="create_task", then2="dispatch_agent")
        return Action(action="create_task", band=2, then="dispatch_agent")

    # Rule 11: Decision → update task with user's choice
    if intent == "decision":
        if state.awaiting_user_decision:
            return Action(action="update_task", band=2)
        return Action(action="respond_only", band=1)

    # Rule 12: Clarification → retry original action with new info
    if intent == "clarification":
        return resolve_with_clarification(event, state)

    # Rule 13: Command → execute
    if intent == "command":
        cmd = parse_command(event)
        if cmd.band == 3 and not cmd.confirmed:
            return Action(action="escalate_to_user", band=1, reason="destructive_command")
        return Action(action="execute_command", band=cmd.band)

    # Fallback: ask
    return Action(action="ask_clarification", band=1)
```

### Chained Actions

Some decisions produce multiple actions in sequence. The `then` field chains them:

```
dispatch intent → promote_note → create_task → dispatch_agent
question intent → create_task → dispatch_agent
```

Each action in the chain produces its own journal entry. If any action fails, the chain stops and the failure is logged.

### Agent Routing

```python
def route_to_agent(event: DecisionEvent) -> str:
    """Deterministic routing based on task type."""
    task_type = infer_task_type(event)

    ROUTING = {
        "architecture":    "ARCH",
        "design":          "ARCH",
        "tradeoff":        "ARCH",
        "review":          "ARCH",
        "implementation":  "FORGE",
        "code":            "FORGE",
        "prototype":       "FORGE",
        "fix":             "FORGE",
        "research":        "ARCH",
        "scan":            "ARCH",
        "summarize":       "ARCH",
        "compare":         "ARCH",
    }

    # Keyword match from classification + event text
    for keyword, agent in ROUTING.items():
        if keyword in task_type or keyword in event.source.raw_input.lower():
            return agent

    # Default: ARCH for unclear work (better to over-think than under-think)
    return "ARCH"
```

### Budget Gate

```python
def budget_allows(state: SystemState, target: str) -> bool:
    """Check if dispatch is within budget."""
    # All agents (ARCH/FORGE) use Claude = cloud tokens
    # No free agent dispatch anymore (Sage removed)

    daily_spent = state.journal.cloud_tokens_today()
    per_task_limit = state.config.per_task_token_limit
    daily_limit = state.config.daily_cloud_token_limit

    if daily_spent + per_task_limit > daily_limit:
        return False  # Would exceed daily cap

    return True
```

## Intent Classification Grammar

The LLM does not produce free-form text for classification. It fills a JSON Schema via Ollama's native `format` parameter.

### Ollama Structured Output (JSON Schema)

Ollama natively enforces JSON Schema when passed in the `format` field. No custom JSON Schema needed — this is a maintained, stable interface.

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "intent": {
      "type": "string",
      "enum": ["brainstorm","note","dispatch","question","decision","command","followup","greeting","farewell","acknowledgement","clarification","noop"]
    },
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "target_agent": {
      "type": ["string","null"],
      "enum": ["ARCH","FORGE",null]
    },
    "urgency": {"type": "string", "enum": ["low","medium","high"]},
    "note_action": {"type": "string", "enum": ["create","update","none"]},
    "topic": {"type": "string"},
    "references_existing": {"type": "boolean"}
  },
  "required": ["intent","confidence","target_agent","urgency","note_action","topic","references_existing"]
}
```

This schema is passed to Ollama via:
```python
resp = await ollama_client.chat(
    model=config.FAST_MODEL,  # e.g. "gpt-oss:20b"
    messages=messages,
    format=INTENT_SCHEMA      # JSON Schema dict above
)
```

### Example classification outputs

**Input:** "I've been thinking about adding real-time analytics to SportWave"
```json
{
  "intent": "brainstorm",
  "confidence": 0.92,
  "target_agent": "NONE",
  "urgency": "low",
  "note_action": "create",
  "topic": "real-time analytics for SportWave",
  "references_existing": false
}
```

**Input:** "Send this to Arch"
```json
{
  "intent": "dispatch",
  "confidence": 0.97,
  "target_agent": "ARCH",
  "urgency": "medium",
  "note_action": "none",
  "topic": "current topic",
  "references_existing": true
}
```

**Input:** "We'd need mmWave data streaming at 60fps minimum"
```json
{
  "intent": "followup",
  "confidence": 0.89,
  "target_agent": "NONE",
  "urgency": "low",
  "note_action": "update",
  "topic": "real-time analytics for SportWave",
  "references_existing": true
}
```

**Input:** "Go with streaming pipeline"
```json
{
  "intent": "decision",
  "confidence": 0.95,
  "target_agent": "NONE",
  "urgency": "medium",
  "note_action": "none",
  "topic": "analytics approach selection",
  "references_existing": true
}
```

**Input:** "Remind me to call the sensor supplier Friday"
```json
{
  "intent": "command",
  "confidence": 0.94,
  "target_agent": "NONE",
  "urgency": "low",
  "note_action": "create",
  "topic": "call sensor supplier",
  "references_existing": false
}
```

**Input:** "What's the weather?"
```json
{
  "intent": "question",
  "confidence": 0.91,
  "target_agent": "NONE",
  "urgency": "low",
  "note_action": "none",
  "topic": "weather",
  "references_existing": false
}
```

**Input:** "Analyze the tradeoffs of Rust vs Go for our backend"
```json
{
  "intent": "question",
  "confidence": 0.88,
  "target_agent": "ARCH",
  "urgency": "medium",
  "note_action": "none",
  "topic": "Rust vs Go backend tradeoffs",
  "references_existing": false
}
```

## The Classification Prompt

This is what Qwen 14B sees on every turn. ~800 tokens.

```
You are Ada's intent classifier. Given the user's message and current context, classify the intent by filling the JSON schema exactly.

RULES:
- EXACTLY ONE intent per message
- If the user says "save this AND send to Arch" → intent is "dispatch" (higher commitment wins)
- If you are NOT SURE → set confidence below 0.6
- "brainstorm" = exploring, no action requested
- "note" = explicitly saving for later ("save this", "remember this")
- "dispatch" = explicitly requesting agent work ("send to", "have Forge", "ask Arch")
- "question" = asking for information ("what do you think?", "is this feasible?")
- "decision" = choosing between options ("go with X", "yes that one", "option B")
- "command" = direct instruction ("remind me", "cancel that", "schedule")
- "followup" = adding to the current topic ("also", "and we'd need", "another thing")
- target_agent: ARCH for architecture/design/research, FORGE for code/implementation, NONE if not agent work
- references_existing: true if this refers to an ongoing topic, false if new topic

CURRENT CONTEXT:
Active topic: {active_topic or "none"}
Pending tasks: {pending_task_summaries}
Last intent: {last_intent}
Session duration: {session_duration}

USER MESSAGE: {user_message}

Respond with ONLY the JSON. No other text.
```

## The Response Generation Prompt

After classification and action resolution, if the action includes `speak`, Ada needs to generate the spoken response. This is a SEPARATE LLM call from classification.

```
You are Ada, Daniel's executive assistant. Generate a spoken response.

PERSONALITY:
- Warm, concise, no filler
- Never recap unless asked
- Acknowledge fast, explain only what matters
- If you're dispatching work: confirm what you're doing, don't ask permission

ACTION BEING TAKEN: {resolved_action}
CONTEXT: {relevant_context}
USER SAID: {user_message}

Respond with what Ada should SAY. Keep it under 2 sentences. Be natural.
```

### Why two calls, not one?

1. **Classification must be grammar-constrained.** Response generation cannot be.
2. **Classification can be cached/reused.** Response is unique per turn.
3. **If classification fails, we can retry without regenerating response.**
4. **Classification is ~100 tokens. Response is ~50 tokens. Total ~150 tokens per turn on Qwen 14B. Fast.**

### Latency budget

```
End of utterance detected
  → Classification call: ~150ms (Qwen 14B, 100 tokens, grammar-constrained)
  → Action resolution: ~1ms (pure code, no LLM)
  → Response generation: ~200ms (Qwen 14B, 50 tokens, streaming)
  → TTS first chunk: ~100ms (Kokoro, streaming)
  ─────────────────────────────
  Total to first audio: ~450ms (well under 1s target)
```

## 20 Conversations Traced Through the Engine

### Conversation 1: Simple brainstorm

```
USER: "I've been thinking about adding real-time analytics to SportWave"

Decision Event:
  source: user_voice
  classification: {intent: "brainstorm", confidence: 0.92, target: NONE, note_action: create}
  action_resolver: → create_note (band 1)
  execution:
    tool: save_note("real-time analytics for SportWave", type=actionable, status=captured)
    tool: speak("Interesting. Let me capture that.")
    state: PROCESSING_FAST → SPEAKING
  side_effects: [note_created:note_0044, noteboard_updated, spoken_response]
```

### Conversation 2: Followup on brainstorm

```
USER: "We'd need mmWave data streaming at 60fps minimum"

Decision Event:
  source: user_voice
  classification: {intent: "followup", confidence: 0.89, target: NONE, note_action: update, references_existing: true}
  action_resolver: → update_note (band 1) [active note_0044 exists on topic]
  execution:
    tool: update_noteboard(note_0044, append="mmWave streaming at 60fps")
    tool: speak("Got it, adding that.")
  side_effects: [note_updated:note_0044, noteboard_updated, spoken_response]
```

### Conversation 3: Question that needs specialist

```
USER: "What do you think, is this feasible?"

Decision Event:
  source: user_voice
  classification: {intent: "question", confidence: 0.88, target: ARCH, urgency: medium}
  action_resolver: → needs_specialist=true, has_note_for_topic=true
    → promote_note(note_0044) → create_task → dispatch_agent(ARCH)
    → budget_allows(ARCH)=true (0 tokens spent today)
  execution:
    tool: promote_note(note_0044, status=promoted_to_task)
    tool: create_task(tsk_001, type=architecture, owner=ARCH, brief="Evaluate feasibility...")
    tool: dispatch_agent(ARCH, task_packet)
    tool: speak("Good architecture question. Let me check with Arch.")
    state: voice PROCESSING_FAST → SPEAKING, executive IDLE → DISPATCHING → WAITING_BACKGROUND
  side_effects: [note_promoted, task_created:tsk_001, agent_dispatched:ARCH, spoken_response]
  budget: cloud_tokens_used: 0 (dispatch is async, tokens counted on delivery)
```

### Conversation 4: Direct command

```
USER: "Remind me to call the sensor supplier Friday"

Decision Event:
  source: user_voice
  classification: {intent: "command", confidence: 0.94, note_action: create}
  action_resolver: → execute_command(schedule, "call sensor supplier", "2026-04-04")
  execution:
    tool: save_note("Call sensor supplier", type=scheduled, trigger="2026-04-04T09:00")
    tool: create_task(tsk_002, type=system, origin=voice, status=queued)
    tool: speak("Noted. I'll remind you Friday morning.")
  side_effects: [note_created, task_created:tsk_002, scheduled_trigger_set, spoken_response]
```

### Conversation 5: Farewell

```
USER: "That's it for now"

Decision Event:
  source: user_voice
  classification: {intent: "farewell", confidence: 0.96}
  action_resolver: → respond_only (band 1)
  execution:
    tool: speak("Got it. I'll let you know when Arch responds.")
    state: voice SPEAKING → ATTENTIVE → (2min) → IDLE
  side_effects: [spoken_response]
```

### Conversation 6: Agent delivers result

```
ARCH returns: {task_id: tsk_001, artifact: spec_document, confidence: high}

Decision Event:
  source: agent_result
  classification: {intent: "agent_delivery", confidence: 1.0}
  action_resolver: → validate_result (band 2)
  execution:
    validate gate 1 (structural): spec present ✓
    validate gate 2 (technical): N/A ✓
    validate gate 3 (intent): matches feasibility question ✓
    validate gate 4 (attention): user is IDLE → delayed delivery
    tool: update_task(tsk_001, status=done, artifacts_received=[spec])
  side_effects: [task_updated:tsk_001, result_validated, delivery_queued]
  budget: cloud_tokens_used: 4200 (Arch's usage), cumulative: 4200
```

### Conversation 7: Delivering stored result

```
USER: "Hey Ada" (next session)

Decision Event:
  source: user_voice
  classification: {intent: "greeting", confidence: 0.98}
  action_resolver: → respond_only (band 1) + check pending deliveries
    → has_pending_delivery(tsk_001)=true → deliver_result
  execution:
    tool: speak("Welcome back. Arch came back on the analytics idea...")
    tool: update_task(tsk_001, status=delivered)
    state: voice IDLE → ATTENTIVE → LISTENING → PROCESSING_FAST → SPEAKING
    state: executive IDLE → (delivery check) → IDLE
  side_effects: [spoken_response, task_delivered:tsk_001]
```

### Conversation 8: User makes a decision

```
USER: "Go with streaming pipeline, tell Forge to prototype it"

Decision Event:
  source: user_voice
  classification: {intent: "decision", confidence: 0.95, target: FORGE}
  action_resolver: → update_task(tsk_001, decision=streaming) + create_task(tsk_003) + dispatch_agent(FORGE)
    → budget_allows(FORGE)=true (4200/100000)
  execution:
    tool: update_task(tsk_001, user_decision="streaming pipeline")
    tool: create_task(tsk_003, type=implementation, owner=FORGE, brief="Prototype streaming pipeline per Arch spec...")
    tool: dispatch_agent(FORGE, task_packet_003)
    tool: speak("Done. Forge is on it. I'll validate the code when he's done.")
  side_effects: [task_updated:tsk_001, task_created:tsk_003, agent_dispatched:FORGE, spoken_response]
```

### Conversation 9: Validation failure → retry

```
FORGE returns: {task_id: tsk_003, artifact: code, confidence: medium}

Decision Event:
  source: agent_result
  classification: {intent: "agent_delivery", confidence: 1.0}
  action_resolver: → validate_result (band 2)
  execution:
    validate gate 1 (structural): code present ✓
    validate gate 2 (technical): tests FAIL — TypeError line 42 ✗
    → retry_count=0, max=3 → retry_dispatch (band 2)
    tool: update_task(tsk_003, status=validating, retry_count=1)
    tool: dispatch_agent(FORGE, {task_id: tsk_003, feedback: "Tests failed: TypeError line 42. Fix and resubmit."})
  side_effects: [validation_failed:gate_2, retry_dispatched:FORGE, task_updated:tsk_003]
  budget: cloud_tokens_used: 3800, cumulative: 8000
```

### Conversation 10: Retry succeeds

```
FORGE returns: {task_id: tsk_003, artifact: code_v2, confidence: high}

Decision Event:
  source: agent_result
  classification: {intent: "agent_delivery", confidence: 1.0}
  action_resolver: → validate_result (band 2)
  execution:
    validate gate 1: code present ✓
    validate gate 2: tests pass ✓, compiles ✓, 28fps ✓
    validate gate 3: implements streaming pipeline per spec ✓
    validate gate 4: quality sufficient, user IDLE → delayed delivery
    tool: update_task(tsk_003, status=done, artifacts_received=[code_v2])
  side_effects: [result_validated, delivery_queued, task_completed:tsk_003]
  budget: cloud_tokens_used: 4100, cumulative: 12100
```

### Conversation 11: Budget near limit

```
USER: "Have Arch redesign the entire sensor fusion pipeline"

Decision Event:
  source: user_voice
  classification: {intent: "dispatch", confidence: 0.93, target: ARCH, urgency: medium}
  action_resolver: → dispatch_agent
    → budget_allows(ARCH)=false (cumulative: 92000, daily limit: 100000, estimated cost: 15000)
    → escalate_to_user, reason: "budget_threshold"
  execution:
    tool: speak("That would put us over today's cloud budget. We've used 92K of 100K tokens. Want me to send it anyway, or queue it for tomorrow?")
    state: executive → AWAITING_USER_DECISION
  side_effects: [spoken_response, escalation:budget_threshold]
```

### Conversation 12: Ambiguous input

```
USER: "Maybe we should rethink the whole thing"

Decision Event:
  source: user_voice
  classification: {intent: "brainstorm", confidence: 0.54, target: NONE}
  action_resolver: → confidence < 0.6 → ask_clarification
  execution:
    tool: speak("Rethink the analytics pipeline, or something bigger? Want to talk it through?")
  side_effects: [spoken_response, clarification_requested]
```

### Conversation 13: Clarification received

```
USER: "The analytics pipeline, yeah. Maybe batch instead of streaming."

Decision Event:
  source: user_voice
  classification: {intent: "clarification", confidence: 0.87, references_existing: true}
  action_resolver: → resolve_with_clarification → update_note + respond
  execution:
    tool: update_noteboard(note_0044, append="reconsidering: batch vs streaming")
    tool: speak("Got it. Want me to have Arch compare batch vs streaming?")
  side_effects: [note_updated, spoken_response]
```

### Conversation 14: System event — Ollama goes down

```
Health check: Ollama unreachable

Decision Event:
  source: system_event
  classification: {intent: "system_event", confidence: 1.0}
  action_resolver: → resolve_system_event → enter DEGRADED
  execution:
    state: overlay → DEGRADED
    tool: speak("My brain just went offline. I can still take notes if you need me.")
    tool: journal_log("ollama_down", severity="warning")
  side_effects: [state_degraded, spoken_response]
```

### Conversation 15: Degraded mode — user talks anyway

```
USER: "Save this — check if Qwen 3 supports grammar mode"

Decision Event:
  source: user_voice
  classification: DEGRADED — using keyword fallback
    keywords: "save" → intent: note
  action_resolver: → create_note (band 1, no LLM needed for storage)
  execution:
    tool: save_note("check if Qwen 3 supports grammar mode", type=personal, status=captured)
    tool: speak("Saved.") [if TTS still works]
  side_effects: [note_created, spoken_response]
```

### Conversation 16: Complex brainstorm → UltraPlan

```
USER: "I need a complete redesign of the BlackDirt physics engine from first principles"

Decision Event:
  source: user_voice
  classification: {intent: "brainstorm", confidence: 0.90, urgency: medium}
  action_resolver: → is_complex_enough_for_ultraplan=true
    (keywords: "complete redesign", "from first principles", domain: blackdirt)
    → queue_ultraplan (band 2)
  execution:
    tool: save_note("BlackDirt physics engine redesign from first principles")
    tool: queue_ultraplan(title="BlackDirt physics redesign", domain="blackdirt", use_aristotle=true)
    tool: speak("That's a big one. I'll plan it overnight with deep thinking. You'll have options in the morning.")
  side_effects: [note_created, ultraplan_queued, spoken_response]
```

### Conversation 17: Morning briefing with UltraPlan results

```
Timer: 07:00 morning briefing

Decision Event:
  source: timer
  classification: {intent: "timer_trigger", confidence: 1.0}
  action_resolver: → execute_command(morning_briefing)
  execution:
    tool: compile_briefing() → "6 plans overnight, 4 solid, 2 need input"
    [queued for delivery on next greeting]
  side_effects: [briefing_compiled, delivery_queued]

USER: "Hey Ada"

Decision Event:
  source: user_voice
  classification: {intent: "greeting"}
  action_resolver: → respond_only + check pending deliveries
    → has_briefing=true, has_ultraplan_results=true
  execution:
    tool: speak("Good morning. I ran 6 plans overnight. The BlackDirt physics redesign has a solid three-pass plan. Two plans need your input. Want to review?")
  side_effects: [spoken_response, briefing_delivered]
```

### Conversation 18: Note that stays a note

```
USER: "Just a thought — I wonder if we could use AdaOS for the legal case too"

Decision Event:
  source: user_voice
  classification: {intent: "note", confidence: 0.91, note_action: create, references_existing: false}
  action_resolver: → create_note (band 1)
  execution:
    tool: save_note("Could AdaOS be used for legal case?", type=personal, status=captured, visibility=personal)
    tool: speak("Saved for you. Not sharing with the team.")
  side_effects: [note_created:personal, spoken_response]
```

### Conversation 19: Max retries exhausted

```
FORGE returns: {task_id: tsk_005, artifact: code_v3, confidence: low}

Decision Event:
  source: agent_result
  classification: {intent: "agent_delivery", confidence: 1.0}
  action_resolver: → validate_result
  execution:
    validate gate 2: tests FAIL again
    → retry_count=3, max=3 → STOP
    tool: update_task(tsk_005, status=failed)
    → delivery class: immediate (failure report)
    tool: speak("Forge couldn't solve this after 3 attempts. The issue is [X]. Want me to escalate to Arch for a different approach?")
    state: executive → AWAITING_USER_DECISION
  side_effects: [validation_failed, max_retries_exhausted, task_failed:tsk_005, escalation:retry_exhausted]
```

### Conversation 20: Proactive Ada — drift detection

```
(After 3 followups on different topics without concluding the first)

Decision Event:
  source: system (proactive behavior trigger)
  classification: {intent: "system_event", subtype: "drift_detection"}
  action_resolver: → respond_only (proactive nudge)
  execution:
    tool: speak("Hey, we started on analytics, then jumped to physics and now sensors. Want me to save these as separate notes, or are they connected?")
  side_effects: [spoken_response, proactive_nudge]
```

## What This Design Gives You

1. **Every input has exactly one Decision Event.** No ambiguity about what happened.
2. **LLM classifies. Code decides.** Clean separation of intelligence and policy.
3. **Every decision is logged.** Full replay, debugging, budget tracking.
4. **Latency is predictable.** ~450ms to first audio (classification + resolution + response + TTS).
5. **Failures are traceable.** Was it a classification error or a resolution error?
6. **Budget is enforceable.** Code checks budget before every dispatch.
7. **The grammar ensures valid output.** No free-form LLM surprises.
8. **20 conversations prove the model works.** Every realistic scenario traced through.

## What To Validate Next

Before writing code:

1. **Run 20 more conversations mentally.** Find edge cases the model doesn't handle.
2. **Test the JSON Schema on GPT-OSS 20B via Ollama.** Does it produce valid JSON every time?
3. **Measure classification latency.** Is 150ms realistic for grammar-constrained 14B output?
4. **Test the two-call pattern.** Classification → Response. Is the combined latency under 500ms?
5. **Stress test the Action Resolver rules.** Feed it adversarial intents. Does it always produce a safe action?

## Files This Design Maps To

```
ada/
├── decision/
│   ├── event.py           # DecisionEvent dataclass
│   ├── classifier.py      # Intent classification (LLM call with grammar)
│   ├── resolver.py         # Action resolution (pure code, no LLM)
│   ├── intent_schema.json        # JSON Schema file for constrained output
│   └── router.py           # Agent routing logic
├── realtime/
│   ├── intent.py           # → replaced by decision/classifier.py
│   ├── responder.py        # Response generation (second LLM call)
│   └── streamer.py         # Noteboard streaming
```

The `decision/` module is the core of Ada. Everything else is plumbing or persistence.
