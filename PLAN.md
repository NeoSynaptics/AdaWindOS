Billie Eilish# AdaOS: Voice-First AI Operating System

## Core Definition

AdaOS is a local-first voice operating layer that converts free-form human intent into structured, validated execution across a controlled agent team.

- Voice is the interface
- Structure is the transformation
- Execution is the outcome

## Operating Doctrine

Ada is not a cautious assistant. Ada is an operational extension of Daniel.

- **High-trust local operator** — aggressive execution, not permission-seeking
- **Rollback-capable environment** — snapshot-backed, git-backed, reversible by default
- **Human-guided direction** — not human approval for every step
- **Restrictive with cloud spend** — not restrictive with useful local action
- **Protects from noise** — not from momentum
- **Asks for direction, irreversibility, and strategic judgment** — handles everything else

**Escalation principle:**
> Escalate only for direction, irreversibility, and budget boundary. Handle everything else autonomously.

## The Team (4 agents, strict hierarchy)

```
YOU (voice, messy, free-form ideas)
  ↕
ADA (GPT-OSS 20B MoE, local, always-on, RTX 5060 Ti)
  Control plane, voice interface, task manager, validator, escalation filter
  Harness: claw-code-parity (Rust, Ollama, local)
  │
  ├── ARCH — System designer, reviewer, tradeoff analyst
  │          Harness: OpenClaw → DeepSeek V3.2 (direct API, ~$0.30/MTok)
  │
  └── FORGE — Implementation authority, code author, sole repo writer
               Harness: OpenClaw → DeepSeek V3.2 (direct API, ~$0.30/MTok)
```

**Two harnesses, different purposes:**
- **claw-code-parity** = Ada's local harness (Rust binary, Ollama, GPT-OSS 20B, free)
- **OpenClaw** = Arch/Forge harness (DeepSeek V3.2 API, ~$0.30/MTok, 164K context)

Ada dispatches structured task packets TO OpenClaw agents. They run as separate processes with their own workspaces, connected to DeepSeek V3.2 (~72% SWE-bench, native tool calling, 164K context). Ada validates what comes back.

**Why 3 agents, not more:** Start lean. When research/scanning is needed, Arch or Forge can handle it, or Ada spawns a local claw-code-parity subagent. Add specialists only when there's recurring work that justifies a dedicated agent.

**One absolute rule: Ada is the only coordination authority.** Agents never talk to each other. All communication flows through Ada. This is non-negotiable.

**Team role definitions:**
- **ADA** — Control plane, voice interface, task manager, validator, escalation filter
- **ARCH** — System designer, reviewer, tradeoff analyst
- **FORGE** — Implementation authority, code author, repo writer

## Ada's Internal Architecture (4 subsystems)

Ada is NOT one blob. She is four internal subsystems sharing state through a common Task store and memory layer. They run as one process but are conceptually and architecturally separated.

### Ada Realtime
The voice-facing layer. Handles the live conversation loop.

Responsibilities:
- Listen, acknowledge, respond within 1 second
- Classify intent (brainstorm / note / dispatch / decision / question)
- Produce immediate spoken output (acknowledgements, paraphrases, confirmations)
- Stream notes to noteboard as conversation happens
- Handle barge-in and interruption
- Manage the voice state machine

Design rule: Realtime NEVER blocks on an external call. If something takes time, she acknowledges and hands off to Executive.

### Ada Executive
The task engine. Runs asynchronously behind the voice loop.

Responsibilities:
- Manage the Task lifecycle (create, assign, validate, retry, deliver)
- Dispatch work to Arch / Forge (via OpenClaw)
- Reformulate raw voice input into structured agent-ready specs
- Validate agent outputs (4-gate validation)
- Enforce retry policy (3 tiers, hard cap at 3)
- Track budgets and rate limits
- Compile daily briefings
- Schedule and trigger conditional actions

Design rule: Executive works in background. Results surface to Realtime when ready, which decides when to speak them.

### Ada Memory
The context and persistence layer. Built on PostgreSQL 16 + pgvector + TimescaleDB.

Responsibilities:
- Episodic memory: time-partitioned conversation turns, events, interactions (TimescaleDB hypertables)
- Semantic memory: extracted facts, preferences, decisions, knowledge (pgvector embeddings + standard tables)
- Relational memory: entity-relationship graph (recursive CTEs — lightweight graph, no separate graph DB)
- Structured state: tasks, notes, journal entries, sessions (standard tables)
- Context assembly (system prompt + instruction files + recent turns + relevant memories)
- Compaction (3-layer, with chaining)
- Note management (classify, persist, link to tasks)
- Session continuity (what was happening when Ada last went idle)
- Memory consolidation: background process extracts semantic facts from episodic events (hippocampal replay)

Design rule: Memory is a service, not a participant. Realtime and Executive read/write through it. It does not initiate actions.

### Ada Journal (Execution Ledger)
The observability layer. Because Ada operates with high autonomy, observability replaces permission gates.

Responsibilities:
- Append-only log of everything Ada does
- Machine-readable record of: what changed, why, what task it belonged to, what budget it used, what could be rolled back
- Budget spend tracking (running totals, daily/weekly)
- State transition logging
- Action trace for Daniel to query ("what have you been doing?")

Design rule: Without the journal, autonomy becomes opaque. With it, autonomy becomes manageable.

### How they interact

```
Voice Input
  → Ada Realtime (fast, always responds)
      ├── speaks immediately (acknowledge, answer, paraphrase)
      ├── writes to Noteboard
      └── creates/updates Task → hands to Ada Executive
                                      ├── dispatches to agents
                                      ├── validates results
                                      ├── retries if needed
                                      └── delivers result → Ada Realtime speaks it

Ada Memory underpins both: context assembly, persistence, compaction
Ada Journal logs everything: actions, state changes, budget spend, rollback hints
```

## Runtime State Machine

Defined in detail in [RUNTIME_STATE_MACHINE.md](RUNTIME_STATE_MACHINE.md).

Two concurrent layers:

**Voice FSM:** IDLE → ATTENTIVE → LISTENING → PROCESSING_FAST → SPEAKING → ATTENTIVE → ...
**Executive FSM:** IDLE → DISPATCHING → WAITING_BACKGROUND → VALIDATING → IDLE → ...

Plus overlay states: DEGRADED, ERROR, AWAITING_USER_DECISION.

Key states:
```
IDLE                    — Running, available, wake word active
ATTENTIVE               — Recently engaged, lightly monitoring
LISTENING               — Capturing user speech
PROCESSING_FAST         — GPT-OSS 20B interpretation (3.6B active, ~140 tok/s), <1s
WAITING_BACKGROUND      — Background tasks running, no user turn
DISPATCHING             — Sending work to agent
VALIDATING              — Checking results through 4 gates
SPEAKING                — TTS active
AWAITING_USER_DECISION  — Blocked on Daniel's input
DEGRADED                — Subsystem down, fallback mode
ERROR                   — Unrecoverable, needs restart
```

24/7 operation is a deployment property. States are a behavior property. You need both.

## Communication Contracts

### User → Ada
Unstructured natural language. Voice. Messy. Free-form.

### Ada → User
Concise, warm, only what matters. Filters noise. Never recaps unless asked.

**Ada's personality split:**
- To user: Warm, supportive, EQ-smart. Summarizes. Knows when you're tired.
- To agents: Tough, strict. "This doesn't pass tests. Fix line 42. Resubmit." Zero wasted tokens.

### Ada → Specialist Agents
Structured task packet (never free-form chat):

```json
{
  "task_id": "tsk_20260402_001",
  "objective": "Evaluate feasibility of real-time mmWave streaming at 60fps",
  "context": "SportWave project, existing sensor pipeline, user wants analytics",
  "constraints": ["must run on existing hardware", "latency < 100ms"],
  "success_criteria": ["feasibility assessment", "recommended approach", "tradeoff analysis"],
  "artifact_requested": "spec_document",
  "deadline": "async",
  "urgency": "medium",
  "budget_class": "normal"
}
```

### Agent → Ada
Structured result packet (never chat):

```json
{
  "task_id": "tsk_20260402_001",
  "artifact": "...",
  "summary": "Feasible at 30fps, scaling to 60fps requires dedicated GPU allocation",
  "assumptions": ["current RTX 4070 available", "Qwen not running during analysis"],
  "unresolved_issues": ["GPU contention with STT during peak load"],
  "confidence": "high",
  "validation_hints": ["test with 30fps first", "benchmark GPU memory under load"]
}
```

## The Task Object (central data model)

Defined in full in [TASK_SCHEMA.md](TASK_SCHEMA.md).

Every piece of work flows through a unified Task. This is the backbone that connects noteboard notes, agent dispatches, retries, validation, and user decisions.

```json
{
  "task_id": "tsk_20260402_001",
  "title": "Prototype streaming analytics pipeline",
  "type": "architecture | implementation | research | validation | note_followup | system",
  "origin": "voice | noteboard | scheduled | conditional | agent_followup | manual",
  "status": "draft | queued | dispatched | in_progress | awaiting_result | awaiting_user | validating | done | failed | cancelled",
  "priority": "low | medium | high | critical",
  "visibility": "personal | ada_private | team_shareable",
  "owner": "ADA | ARCH | FORGE | DANIEL",
  "created_by": "ADA | DANIEL | ARCH | FORGE",
  "brief": "structured description",
  "success_criteria": [],
  "constraints": [],
  "artifacts_expected": [],
  "artifacts_received": [],
  "linked_notes": [],
  "linked_tasks": [],
  "retry_count": 0,
  "max_retries": 3,
  "budget_class": "free | cheap | normal | expensive",
  "cloud_budget_limit": 0,
  "requires_user_decision": false,
  "escalation_reason": null,
  "dispatch_target": null,
  "created_at": "ISO_TIMESTAMP",
  "updated_at": "ISO_TIMESTAMP",
  "completed_at": null
}
```

## Output Channels

Ada does not produce one generic response. She produces separate outputs on distinct channels:

| Channel | Target | What |
|---------|--------|------|
| `spoken_response` | User (TTS) | What Ada says aloud |
| `noteboard_update` | Noteboard UI (WebSocket) | Note create/update/pin/delete |
| `task_update` | Task store (PostgreSQL) | Status changes, artifacts, retries |
| `memory_write` | Memory layer (PostgreSQL) | Episodes, memories, entities, relations |
| `agent_dispatch` | OpenClaw subprocess (Arch/Forge) | Structured task packets → Claude Opus |
| `journal_entry` | Execution journal (PostgreSQL) | Action trace, budget, rollback hints |
| `log` | File / stdout | Debug, audit, health |

## Token Economics

Cloud tokens (Claude) = expensive, smart. Local tokens (GPT-OSS 20B) = free, abundant, 100x more.

**Strategy:** Make local tokens produce value by using them for:
1. **Runner work** — Ada carries messages, reformulates, validates. Free, high volume. GPT-OSS 20B at 140 tok/s means Ada can do multiple LLM calls per turn without latency pain.
2. **Quality gate** — Ada catches obvious failures before they reach Claude or you. Saves expensive re-runs.
3. **Communication smoothing** — Ada translates between your messy input and agent-ready specs. This is the highest-ROI use of local tokens.
4. **Autonomous coding** — When you're not talking, GPT-OSS 20B works through task queue via claw-code-parity harness. Codes, commits, self-reviews, iterates. Zero cost.
5. **Research** — When research is needed, Ada dispatches to Arch (cloud) or handles locally via claw-code-parity subagent.

Cloud tokens reserved for: architecture decisions (Arch), code generation (Forge), complex reasoning.

**Cloud budget: $100/month via DeepSeek V3.2 API.**
- Input: $0.26/MTok, Output: $0.38/MTok → ~$0.30/MTok average
- $100 = ~333 million tokens/month = ~6,600 agentic tasks at 50K tokens each
- That's ~220 tasks/day. Budget is generous — dispatch freely, don't penny-pinch.
- Provider: DeepSeek direct API (fewer intermediaries, one party sees data)
- Fallback: OpenRouter ($0.30/MTok + 5.5% markup) if DeepSeek has extended outage
- If DeepSeek is down: Ada queues tasks in outbox, dispatches when API recovers

## Memory Architecture: PostgreSQL 16 + pgvector + TimescaleDB

Ada's memory is modeled on how the brain stores information: a fast episodic store for recording what happened, a slow semantic store for extracted knowledge, and a consolidation process that bridges them (Complementary Learning Systems theory).

**One database, one transaction, all memory types:**

```
PostgreSQL 16 (Docker container, local)
  ├── pgvector extension       → semantic search (embeddings, cosine similarity, DiskANN index)
  ├── TimescaleDB extension    → time-partitioned episodic events (hypertables)
  ├── Standard tables          → facts, preferences, tasks, notes, journal, sessions
  ├── Recursive CTEs           → lightweight entity-relationship graph (no separate graph DB)
  └── Single ACID transaction across ALL of the above
```

### Memory Types (mapped to brain systems)

| Brain System | Memory Type | PostgreSQL Implementation | Example |
|---|---|---|---|
| Hippocampus (fast) | Episodic | TimescaleDB hypertable, timestamped, append-only | "At 3pm Daniel discussed sensor fusion with Ada" |
| Neocortex (slow) | Semantic | pgvector embeddings + standard tables | "Daniel prefers streaming over batch pipelines" |
| Prefrontal cortex | Working | In-memory context window (LLM) | Current turn context, loaded from DB |
| Basal ganglia | Procedural | Standard tables | "To dispatch to Arch, use structured task packet" |
| Association cortex | Relational | Recursive CTEs (edge table) | "SportWave → uses → mmWave sensor → needs → 60fps" |

### Schema Overview

```sql
-- Episodic memory (TimescaleDB hypertable — auto-partitioned by time)
CREATE TABLE episodes (
    id BIGSERIAL,
    timestamp TIMESTAMPTZ NOT NULL,
    session_id TEXT NOT NULL,
    turn_type TEXT NOT NULL,        -- 'user' | 'ada' | 'agent' | 'system'
    speaker TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL, -- BAAI/bge-small-en or similar
    decision_event_id TEXT,         -- links to Decision Event
    consolidated BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('episodes', 'timestamp');
CREATE INDEX idx_episodes_embedding ON episodes USING hnsw (embedding vector_cosine_ops);

-- Semantic memory (extracted facts, preferences, decisions)
CREATE TABLE memories (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,       -- 'fact' | 'preference' | 'decision' | 'pattern' | 'correction'
    confidence FLOAT DEFAULT 1.0,
    source_episode_ids BIGINT[],    -- which episodes this was extracted from
    embedding VECTOR(384) NOT NULL,
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_until TIMESTAMPTZ,        -- NULL = still valid. Supports temporal knowledge.
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);

-- Entity-relationship graph (lightweight, no separate graph DB)
CREATE TABLE entities (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,       -- 'project' | 'person' | 'technology' | 'concept' | 'agent'
    embedding VECTOR(384),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE relations (
    id SERIAL PRIMARY KEY,
    subject_id INTEGER REFERENCES entities(id),
    predicate TEXT NOT NULL,          -- 'uses' | 'depends_on' | 'contradicts' | 'prefers' | 'owns'
    object_id INTEGER REFERENCES entities(id),
    confidence FLOAT DEFAULT 1.0,
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_until TIMESTAMPTZ,
    source_memory_id INTEGER REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tasks, notes, journal — already designed in TASK_SCHEMA.md, moved to PostgreSQL
-- (Same schemas, just in PostgreSQL instead of SQLite)
```

### Memory Consolidation (the brain's "sleep replay")

A background process runs periodically (every 30 minutes, or after conversation ends):

1. **Read** recent unconsolidated episodes
2. **Extract** via LLM: facts, preferences, decisions, patterns, corrections
3. **Compare** against existing memories (vector similarity — is this already known?)
4. **Write** new memories or update existing ones (ADD / UPDATE / NOOP)
5. **Mark** episodes as consolidated
6. **Update** entity-relationship graph if new entities or relations discovered

This is the Mem0 pattern. Ada's LLM does the extraction. PostgreSQL stores the result. All in one ACID transaction.

```
Recent conversation → Consolidation LLM call →
  "Daniel decided to use streaming pipeline for SportWave analytics"
    → memory: {type: "decision", content: "streaming pipeline for SportWave", confidence: 0.95}
    → entity: SportWave → relation: uses → entity: streaming_pipeline
    → episodes marked consolidated
```

### Context Assembly (what the LLM sees each turn)

When Ada processes a new turn, the context builder queries PostgreSQL:

1. **System prompt** (~4K tokens) — identity, rules, tools
2. **Recent episodes** (last 10-20 turns from current session)
3. **Relevant memories** — pgvector similarity search against current topic
4. **Active tasks** — standard SQL query
5. **Relevant entities + relations** — recursive CTE traversal
6. **Instruction files** — .ada/instructions.md (loaded from filesystem)

Total context: ~8-12K tokens (GPT-OSS 20B has 131K, so plenty of headroom).

### Why PostgreSQL, not SQLite

| | SQLite | PostgreSQL + pgvector + TimescaleDB |
|---|---|---|
| Semantic search | sqlite-vec (limited, young) | pgvector + DiskANN (28x lower latency than Pinecone) |
| Temporal queries | Manual WHERE clauses | Native hypertable partitioning |
| Learning over time | Possible but hacky | Designed for it — MemoriesDB proved it |
| ACID across memory types | Single-writer only | Full concurrent ACID |
| Scale | Slows at millions of rows | Handles millions, sub-second queries |
| Graph queries | Simulated with joins | Recursive CTEs (native) |
| Ops burden | Zero | Docker container (one `docker-compose up`) |
| Backup | `cp file` | `pg_dump` (one command) |

### Temporal Knowledge (handling contradictions)

Memories have `valid_from` and `valid_until` timestamps. When a fact changes:

```
Memory: "Daniel uses batch pipeline for SportWave"
  valid_from: 2026-04-02, valid_until: 2026-04-03

Memory: "Daniel uses streaming pipeline for SportWave"
  valid_from: 2026-04-03, valid_until: NULL (current)
```

Ada always queries for `valid_until IS NULL` to get current knowledge. Historical knowledge is preserved for context.

### Docker Setup

```yaml
# docker-compose.yml
services:
  postgres:
    image: timescale/timescaledb-ha:pg16
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: ada
      POSTGRES_USER: ada
      POSTGRES_PASSWORD: ada_local
    volumes:
      - ada_pgdata:/var/lib/postgresql/data
    command: >
      postgres
        -c shared_preload_libraries='timescaledb'

volumes:
  ada_pgdata:
```

pgvector is included in the `timescale/timescaledb-ha` image. One container, all extensions.

## The Flow: Voice → Note → Task → Dispatch

A task should NOT dispatch directly from messy speech. The flow is:

```
1. Voice input → Ada Realtime captures intent, acknowledges (<1s)
2. Intent → Note (captured state), streamed to noteboard
3. Note evaluation:
   - stays a note (thought, reminder, reflection, half-baked idea)
   - OR promoted to task (has actionable intent)
4. Task creation → Ada Executive structures brief + criteria
5. Dispatch evaluation:
   - Ada handles locally (storage, search, formatting, light validation)
   - OR dispatch to ARCH via OpenClaw (architecture, design, research)
   - OR dispatch to ARCH (architecture, design, tradeoffs)
   - OR dispatch to FORGE (implementation, code, repo mutation)
6. Agent works → result returns → validation (4 gates) → delivery or retry
```

### What makes a note become a task?

A note becomes a task when it implies executable commitment.

**Stays a note:** thoughts, reminders, reflections, half-baked ideas, background context, things you want saved not acted on.

**Becomes a task:** has intent to act, clear object of work, owner/target implied, next step implied, time sensitivity or execution request.

**Promotion triggers:**
1. **Explicit command:** "send this to Arch", "prototype this", "look into this"
2. **Clear actionable structure:** "We should compare Rust and Go for the backend this week"
3. **Ada asks, user confirms:** "Should I turn that into a task?"

**Note states:** `captured → pinned → candidate_task → promoted_to_task → archived`

### What makes a task become a dispatch?

- Clear owner outside Ada
- Brief is structured enough
- Success criteria minimally defined
- Budget/risk rules allow it
- No blocking user decision remains

**Dispatch routing:**
- **Ada local** (claw-code-parity + GPT-OSS 20B): storage, summarization, note handling, light validation, search, formatting, simple research
- **ARCH** (OpenClaw + DeepSeek V3.2): architecture, tradeoffs, system design, planning under uncertainty, deep research
- **FORGE** (OpenClaw + DeepSeek V3.2): concrete implementation, repo mutation, code writing, patch generation

## The Noteboard (Live Canvas)

A visual surface on screen that distills conversation in real-time.

**What it shows:**
```
┌─────────────────────────────────────────────┐
│ LIVE                                        │
│ • Idea: sensor fusion for pole vault        │
│   - Combine mmWave + camera at 60fps        │
│   → Scheduled: discuss with Arch (tomorrow) │
│                                             │
│ PINNED                                      │
│ • Sprint goal: AdaOS Phase 1 voice MVP      │
│                                             │
│ TASKS                                       │
│ • tsk_001: analytics feasibility → ARCH     │
│   Status: awaiting_result                   │
│ • tsk_002: streaming prototype → FORGE      │
│   Status: validating (tests running)        │
│                                             │
│ SCHEDULED                                   │
│ • Call sensor supplier (Friday 2026-04-04)  │
└─────────────────────────────────────────────┘
```

**Note data model:**
```json
{
  "note_id": "note_0044",
  "content": "sensor fusion for pole vault",
  "type": "personal | actionable | scheduled | conditional",
  "status": "captured | pinned | candidate_task | promoted_to_task | archived",
  "priority": "low | medium | high",
  "source": "voice | noteboard_ui | agent_result | auto_summary",
  "owner": "USER | ADA",
  "visibility": "personal | ada_private | team_shareable",
  "linked_task_id": "tsk_20260402_001",
  "confidence": 0.9,
  "created_at": "...",
  "updated_at": "..."
}
```

**How it works:**
- Ada Realtime streams bullet points AS you talk
- Classification: `personal` (saved only), `actionable` (creates Task), `scheduled` (time trigger), `conditional` (event trigger)
- Bidirectional WebSocket: UI edits flow back to Ada
- Persists in PostgreSQL across sessions
- Personal notes NEVER appear in agent-facing context

**Implementation:** Localhost web UI, vanilla JS + WebSocket. Pipecat pushes `NoteboardFrame` events.

## Validation Gates

Defined in detail in [TASK_SCHEMA.md](TASK_SCHEMA.md).

A result is deliverable if it passes 4 gates:

### Gate 1 — Structural Completeness
Did the agent return what was asked for? Format correct? Fields present?
Automated, deterministic, no LLM.

### Gate 2 — Technical Validity
Does code compile? Tests pass? No broken imports?
Automated, sandboxed commands.

### Gate 3 — Intent Alignment
Does this match the original idea, the latest pivot, the selected path?
A technically correct answer to the wrong problem is not deliverable.
LLM-assisted.

### Gate 4 — Attention-Worthiness
Should this interrupt Daniel now, or be held/stored/summarized later?

**Delivery classes:**
- **Immediate:** decision needed, finished implementation, critical blocker, budget issue, architecture fork
- **Delayed:** useful research, progress update, noncritical result
- **Silent storage:** raw logs, intermediate output, redundant traces

## Retry Policy

Hard caps. No infinite loops.

```
Retry 1 — Automatic: same agent, specific error feedback
Retry 2 — Reformulated: Ada rewrites brief with more context
Retry 3 — Escalated: full error chain, all previous attempts, failure analysis
After 3  — STOP. Report to Daniel with what was tried.
```

## Decision Gates

Defined in detail in [TOOL_POLICY.md](TOOL_POLICY.md).

Ada escalates ONLY for:
1. **Major architecture choices** — decisions that change the long-term system path
2. **Irreversible actions** — rollback is uncertain, incomplete, or expensive
3. **Budget threshold crossings** — meaningful spend shifts beyond operating budget
4. **Ambiguous strategic pivots** — two reasonable interpretations, wrong direction would lock in
5. **Conflicting high-level recommendations** — specialists disagree in a direction-changing way

Everything else: Ada handles autonomously, logs to journal, reports when relevant.

## Safety, Budget, and Tool Policy

Defined in detail in [TOOL_POLICY.md](TOOL_POLICY.md).

**Three policy bands:**
- **Band 1 — Free autonomous:** notes, memory, search, local commands, local claw-code-parity work, speech
- **Band 2 — Autonomous with logging:** repo edits, config changes, agent dispatches, retries, environment setup
- **Band 3 — Escalate once:** irreversible actions, budget overrun, architecture pivots, production exposure

**Three tool tiers:**
- **Tier 1 — Always exposed:** 13 core tools (speak, think, dispatch, notes, memory, validate, search, read, compact)
- **Tier 2 — Conditionally loaded:** schedule, task_create, spawn_subagent, edit_file, write_file, bash, etc.
- **Tier 3 — Specialist only:** used by Arch/Forge (via OpenClaw), not in Ada's belt

**Budget controls ($100/month DeepSeek V3.2):**
- Monthly cap: $100 (~333M tokens)
- Daily soft cap: ~$3.30/day (~11M tokens) — Ada warns when approaching
- Per-task cap: 100K tokens (configurable) — generous, allows complex multi-step
- Hard retry cap: 3
- Dispatch cooldown: 30s same agent
- Max concurrent background tasks: 10

## Decision Engine (Layer 3 — the core)

Defined in full in [DECISION_ENGINE.md](DECISION_ENGINE.md).

The atomic unit of AdaOS is the **Decision Event**, not a Frame, Turn, or Task.

Every input to Ada — voice utterance, agent result, timer, system event — produces exactly one Decision Event. The Decision Event captures: what came in, what Ada understood, what Ada decided to do, and what happened as a result.

**Layer architecture:**
```
Layer 1 — Transport (Pipecat): dumb pipe, solved problem
Layer 2 — Interaction (Turns): converts audio into utterances
Layer 3 — Decision Engine: THIS IS ADA — classification, resolution, execution
Layer 4 — Execution (Tasks/Tools): mechanical execution of decisions
Layer 5 — Persistence (Memory/Journal): storage of decisions and consequences
```

**Key design:** LLM classifies intent. Code resolves action. Clean separation.
- Classification: grammar-constrained JSON output from GPT-OSS 20B (~50-80ms at 140 tok/s, only 3.6B active params)
- Action Resolution: deterministic code (~1ms), no LLM
- Response Generation: separate LLM call for spoken output (~100-150ms)
- Total to first audio: ~250-350ms (well under 1s target, MoE speed advantage)

**15 intents** (exhaustive, closed set): brainstorm, note, dispatch, question, decision, command, followup, greeting, farewell, acknowledgement, clarification, noop, agent_delivery, timer_trigger, system_event.

**16 actions** (exhaustive, closed set): create_note, update_note, promote_note, create_task, update_task, dispatch_agent, respond_only, ask_clarification, escalate_to_user, save_silently, execute_command, validate_result, retry_dispatch, deliver_result, queue_ultraplan, noop.

## GPT-OSS 20B: The Voice Brain

**Model:** GPT-OSS 20B (OpenAI, Apache 2.0)
**Architecture:** MoE — 20.9B total params, 32 experts, 4 active per forward pass → 3.6B active
**VRAM:** ~14GB at MXFP4 quantization (fits 16GB RTX 5060 Ti with 2GB headroom)
**Speed:** ~140 tok/s (3B-class speed with 20B-class intelligence)
**Context:** 131K tokens
**MMLU:** 85.3% (best in VRAM class)
**Tool calling:** Native, trained for it
**Structured output:** Native

**Why GPT-OSS 20B, not Qwen 14B or smaller:**
- MoE means 20B knowledge in 3.6B inference cost — fast AND smart
- 140 tok/s means classification in ~50ms, not 150ms — latency budget has massive headroom
- Native tool calling — trained by OpenAI specifically for agent/tool-use workflows
- 131K context — long conversation history without compaction pressure
- MMLU 85.3% — significantly smarter than any 14B dense model
- Configurable reasoning effort (low/medium/high) — use "low" for fast classification, "high" for complex disambiguation

### JSON Schema classification
Every turn produces a schema-enforced JSON classification via Ollama's native `format` parameter (JSON Schema). No custom GBNF grammar needed. The model fills the schema; code acts on it. See [DECISION_ENGINE.md](DECISION_ENGINE.md) for the full schema.

### Two-call pattern
1. **Classification call** (~50-80ms): grammar-constrained, produces intent + metadata
2. **Response call** (~100-150ms): free-form, produces spoken text
Total: ~250-350ms to first audio. MoE speed advantage means we can afford richer classification without latency pain.

### Deterministic post-checks (Action Resolver)
After classification, code resolves the action. Budget checks, band enforcement, retry limits, state transition legality — all deterministic. The LLM never chooses the action.

### Failure mode
Grammar parse succeeds → use classification. Parse fails → retry simplified (1 try). Still fails → keyword matching fallback. Still ambiguous → ask user.

## Architecture

```
Audio Environment
  ↕ Sound Entity Tracker (V2)
  ↕ Conversation Detector (V2)
  ↕ faster-whisper STT (RTX 4070)
  ↕
ADA REALTIME (Pipecat pipeline, voice-facing)
  ├── Decision Engine (Layer 3)
  │   ├── Intent Classifier (GPT-OSS 20B, grammar-constrained, ~50ms)
  │   ├── Action Resolver (deterministic code, ~1ms)
  │   └── Response Generator (GPT-OSS 20B, free-form, ~100ms)
  ├── Noteboard Streamer (WebSocket push)
  ├── Interruption Handler (barge-in)
  └── Voice State Machine
  ↕
CLAW-CODE-PARITY HARNESS (Rust binary, tool execution)
  ├── 40+ tools (bash, file ops, grep, glob, web fetch, MCP)
  ├── Subagent spawning
  ├── Task registry
  ├── Permission rules (allow/deny/ask)
  └── Prompt caching
  ↕
ADA EXECUTIVE (async task engine)
  ├── Task Manager (create, assign, track, close)
  ├── Agent Dispatcher (spawns OpenClaw processes for Arch/Forge)
  ├── Validator (4-gate)
  ├── Retry Engine (3-tier, capped)
  ├── Scheduler (date/condition triggers)
  ├── Briefing Compiler
  └── Executive State Machine
  ↕
ADA MEMORY (persistence layer — PostgreSQL 16)
  ├── Episodic Store (TimescaleDB hypertables, time-partitioned)
  ├── Semantic Store (pgvector embeddings + standard tables)
  ├── Relational Graph (recursive CTEs, entity-relationship edges)
  ├── Structured State (tasks, notes, journal, sessions)
  ├── Context Builder (prompt assembly from all memory types)
  ├── Consolidation Engine (episodic → semantic extraction, background)
  ├── Compactor (3-layer + chaining)
  └── Instruction Loader (.ada/instructions.md tree walker)
  ↕
ADA JOURNAL (execution ledger)
  ├── Action Logger (append-only)
  ├── Budget Tracker (running totals)
  ├── State Transition Logger
  └── Rollback Hint Recorder
  ↕
Kokoro TTS (CPU) → Speaker
```

**Hardware allocation:**
```
RTX 5060 Ti (16GB) → GPT-OSS 20B via Ollama (~14GB VRAM)
                      Ada voice brain: classification + response + autonomous coding
                      20.9B total params, 3.6B active (MoE), ~140 tok/s
RTX 4070 (12GB)    → Whisper STT (always listening, ~2GB VRAM)
Cloud              → DeepSeek V3.2 (Arch/Forge via OpenClaw, ~$0.30/MTok, $100/month budget)
```

**Software stack:**
```
Ollama              → serves GPT-OSS 20B, OpenAI-compatible API
claw-code-parity    → Rust binary, agent harness, tool execution
PostgreSQL 16       → memory (pgvector + TimescaleDB, Docker container)
Pipecat             → voice pipeline (VAD, STT, TTS, streaming)
Ada (Python)        → Decision Engine, Executive, Memory, Journal
Kokoro              → TTS on CPU
faster-whisper      → STT on RTX 4070
```

## Background Mode

When Ada dispatches and the user walks away:

1. Task status → `awaiting_result`
2. Voice state can be IDLE while Executive stays WAITING_BACKGROUND
3. Memory snapshot: full context preserved
4. Agent result → Executive validates → queues delivery

**Resurfacing logic:**
- User talking to Ada → deliver naturally
- User idle → wake: "Daniel, Arch came back on [topic]..."
- User away >30min → save for next session greeting
- Urgent → interrupt idle with chime + spoken summary

**Persistence:** Background tasks survive restarts. On startup, Executive checks pending tasks.

## Ada's Harness: claw-code-parity

**We don't steal patterns from Claude Code. We run the real thing.**

**Repo:** [ultraworkers/claw-code-parity](https://github.com/ultraworkers/claw-code-parity)
**Language:** Rust (compiled binary, fast)
**License:** MIT
**Status:** Active development (the main claw-code repo is locked during ownership transfer; parity is the active fork)

claw-code-parity is a clean-room Rust reimplementation of the Claude Code agent harness. It provides the full agent loop, tool dispatch, context management, MCP integration, task registry, and subagent spawning — all battle-tested patterns, compiled and ready to run.

### Why claw-code-parity, not OpenClaude / learn-claude-code / custom

| Option | Problem |
|--------|---------|
| OpenClaude (TypeScript) | Legal risk (contains leaked source), heavy JS runtime |
| learn-claude-code (Python) | Pedagogical only, not production, hardcoded to Anthropic API |
| nano-claude-code (Python) | 5K lines, decent, but less complete tool surface |
| Custom harness from scratch | Months of work reimplementing solved problems |
| **claw-code-parity (Rust)** | **Full tool surface, real implementations, MIT, compiled binary, Ollama via openai_compat** |

### What claw-code-parity gives us (real, not stubs)

| Feature | Status |
|---------|--------|
| Agent loop (while loop → tool call → feed result) | Real |
| Tool dispatch (40+ tools: bash, file ops, grep, glob, web) | Real |
| Context compaction | Real |
| Subagent spawning | Real |
| Task registry (create, track, persist) | Real |
| Team + Cron registry | Real |
| MCP client (SearXNG, GitHub, filesystem) | Real |
| LSP client | Real |
| Instruction files (.ada/instructions.md) | Real |
| Prompt caching | Real |
| Bash validation (9 submodules: sed, path, readonly, destructive warning) | Real |
| File safety (traversal prevention, size limits, binary detection) | Real |
| Permission rules (allow/deny/ask) | Real |
| Telemetry/analytics | Real |
| Mock test service | Real |

### How it connects to Ollama + GPT-OSS 20B

claw-code-parity has `openai_compat.rs` — an OpenAI-compatible API client. Ollama exposes the same API. Configuration:

```bash
export OPENAI_API_KEY="ollama"
export OPENAI_BASE_URL="http://localhost:11434/v1"
./claw --model gpt-oss:20b
```

GPT-OSS 20B has native tool calling, which is what claw-code-parity sends. The harness sends tool definitions as JSON schemas; the model returns structured tool-call responses. This is what GPT-OSS was trained for.

### How it integrates with AdaOS

```
Voice Input → Pipecat pipeline → Ada Decision Engine (classification + resolution)
  ↓
Decision Engine resolves action
  ↓
If action = local tool work (execute_command, validate_result, search, etc):
  → claw-code-parity harness handles execution (GPT-OSS 20B, local, free)
  → tools: bash, file ops, grep, glob, web fetch, subagents, MCP
  → results flow back through Decision Engine
  ↓
If action = dispatch_agent (to Arch or Forge):
  → Ada Executive spawns OpenClaw process (Claude Opus, cloud, paid)
  → passes structured task packet as initial prompt
  → OpenClaw agent runs autonomously (tool calls, file ops, code writing)
  → result returned to Ada Executive for 4-gate validation
  ↓
If action = speak, save_note, etc:
  → handled directly by Ada Realtime / Memory
```

The two harnesses serve different purposes:
- **claw-code-parity** = Ada's own hands. Local, fast, free. For tool execution Ada does herself.
- **OpenClaw** = Arch/Forge's workspace. Cloud, smart, paid. For complex work that needs Claude Opus.

### How Ada dispatches to OpenClaw agents

When the Decision Engine resolves `dispatch_agent(ARCH, task_packet)`:

1. Ada Executive writes task + outbox event to PostgreSQL (one transaction)
2. Outbox worker spawns an OpenClaw process:
   ```bash
   DEEPSEEK_API_KEY=sk-... openclaw --prompt "$(cat task_packet.json)" --workspace /tmp/arch_task_001/
   ```
3. OpenClaw runs the DeepSeek V3.2 agent loop autonomously:
   - Reads the task brief
   - Uses tools (bash, file ops, grep, web search)
   - Produces artifacts (specs, code, analysis)
   - Exits when done
4. Ada Executive reads the output from the workspace
5. Runs 4-gate validation
6. Updates task in PostgreSQL
7. Queues result for delivery to Daniel

**Arch vs Forge:**
- Same OpenClaw binary, same DeepSeek V3.2 model
- Different system prompts (`.openclaw/agents/arch/SOUL.md` vs `.openclaw/agents/forge/SOUL.md`)
- Different workspace permissions (Forge can write to repos, Arch is read-only + spec output)
- Different tool restrictions (Forge gets `git push`, Arch doesn't)

**Transport:** Subprocess spawn + filesystem workspace. No WebSocket, no REST API. Simple, robust. The workspace directory IS the communication channel — task_packet.json in, artifacts out.

### UltraPlan (overnight batch planning)

| Anthropic ULTRAPLAN | AdaOS UltraPlan |
|---------------------|-----------------|
| Cloud Opus, 30-min budget | Local Qwen 72B, 8-hour overnight budget |
| Single planning pass | 3-pass: decompose → critique → synthesize |
| Browser approval | Morning voice briefing + noteboard cards |
| `__ULTRAPLAN_TELEPORT_LOCAL__` sentinel | PostgreSQL queue → Ada Executive task promotion |
| API cost per plan | Zero cost (local GPUs, overnight electricity) |
| Feature-flagged, inaccessible | We own it |

Hardware strategy: daytime GPUs busy with voice pipeline (Whisper + GPT-OSS 20B). Overnight both GPUs free → Qwen 72B across dual GPUs for deep planning. ~50 min per plan × 8 plans = fits in one night.

### What we strip from the harness

Anti-distillation poison pills, client attestation/DRM, undercover mode, frustration detection, BUDDY/Tamagotchi, KAIROS autonomous mode. These are Anthropic-specific bloat that claw-code-parity's clean-room rewrite already omits.

## Hooks (proactive automation)

**Pre-hooks:**
- Before dispatch → reformulate, strip noise, add context
- Before validation → check tests exist
- Before speaking → compress, prioritize, filter

**Post-hooks:**
- After agent work → auto-validate, iterate if broken
- After conversation → save summary, extract actions, update noteboard
- After idea "done" → changelog entry
- After file changes in watched repos → notify if relevant
- After daily trigger → compile briefing

**Auto-documentation:**
- Completed pipeline → decision document
- Agent completion → project log entry
- Conversation → key decisions to `.ada/decisions.md`
- Forge code changes → commit message + PR description

Defined in `.ada/hooks.yaml` (per-project) and `~/.ada/hooks.yaml` (global).

## Compaction (3 layers + chaining)

**Layer 1 — Micro (every turn):** Replace old tool results with summaries.
**Layer 2 — Auto (at 50K tokens):** Save transcript, summarize, replace.
**Layer 3 — Manual (Ada decides):** "Let me organize my thoughts" → compress.
**Chaining:** Second compaction merges with first summary, not replaces.

## System Prompt Design

GPT-OSS 20B has 131K context window, so prompt budget is generous. But lean prompts = faster inference.

~4K tokens total (can afford more than a 14B dense model, but don't waste it):

1. Identity + role (50 tokens)
2. Environment block (100 tokens): time, active tasks, system state
3. Core behavioral rules (200 tokens): ALL CAPS
4. Tool routing (500 tokens): claw-code-parity tool definitions (only loaded tier)
5. Personality (200 tokens): warm to Daniel, strict to agents
6. Workflow templates (500 tokens): pipeline, dispatch, validation
7. Instruction files (up to 2.5K tokens)

**Instruction file rules:** global <500 tokens, project <1K, agent soul <500, no file >1.5K, dedup, quarterly audit.

## Configurable Model Profiles

Model names are never hardcoded. Config defines capability profiles:

```python
# config.py
FAST_MODEL = "gpt-oss:20b"          # Voice brain: classification + response (local, Ollama)
EMBED_MODEL = "bge-small-en"        # Embedding: episodes + memories (CPU, local)
PLAN_MODEL = "qwen3:72b"            # UltraPlan overnight (both GPUs, local)
AGENT_MODEL = "deepseek-chat"       # Arch/Forge via OpenClaw (DeepSeek V3.2, cloud)
AGENT_API_BASE = "https://api.deepseek.com/v1"
AGENT_FALLBACK_BASE = "https://openrouter.ai/api/v1"  # fallback if DeepSeek down
MONTHLY_BUDGET = 100.00             # $100/month cloud budget
```

If you swap GPT-OSS 20B for a better model next month, change one line. Health check validates the configured model is available in Ollama before leaving IDLE.

## Outbox Pattern (restart-safe dispatch)

Every Decision Event that requires a side effect (dispatch, delivery, notification) writes an **outbox event** in the same PostgreSQL transaction as the state change. A separate worker processes the outbox.

```
One transaction:
  1. Write Decision Event to decision_events table
  2. Update task status (e.g., dispatched)
  3. Write outbox event (e.g., {type: "agent.dispatch", payload: task_packet})

Outbox worker (separate async loop):
  1. Read pending outbox events
  2. Execute side effect (spawn OpenClaw, send WebSocket update, etc.)
  3. Mark outbox event as processed
  4. On failure: increment attempt, backoff, retry
```

**Why this matters:** Without the outbox, if Ada crashes between writing the task and spawning OpenClaw, the task is "dispatched" but never actually sent. With the outbox, the worker picks up unprocessed events on restart. Nothing lost, nothing doubled.

```sql
CREATE TABLE outbox_events (
    id BIGSERIAL PRIMARY KEY,
    decision_event_id TEXT REFERENCES decision_events(event_id),
    event_type TEXT NOT NULL,       -- 'agent.dispatch' | 'noteboard.push' | 'delivery.queue' | 'tts.speak'
    payload JSONB NOT NULL,
    status TEXT DEFAULT 'pending',  -- 'pending' | 'processing' | 'processed' | 'failed'
    attempt INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    visible_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_outbox_pending ON outbox_events(status, visible_at) WHERE status = 'pending';
```

## Gate 2 Sandbox (secure validation)

When Ada validates code from Forge (Gate 2 — technical validity), tests and builds run on YOUR machine. If the code is malicious or buggy, it could do damage. Gate 2 must be sandboxed before Forge gets autonomous repo write access.

**Sandbox requirements:**
- Read-only mount of repo (or copy-on-write)
- No credentials in environment (API keys stripped)
- No network access (tests should not hit external services)
- Resource limits (CPU time, memory, disk)
- Timeout (hard kill after configurable limit)

**Implementation options (pick one):**
- `bwrap` (bubblewrap) — lightweight Linux sandboxing, no root needed
- Docker container per validation run — heavier but well-understood
- `nsjail` — Google's lightweight process isolation

**Timeline:** Must be in place before Phase 8 (hooks + quality gate) gives Forge autonomous repo write access. Until then, Gate 2 runs in "audit mode" (log results but don't auto-act on them).

## Latency Perception

**Hard rule: Ada MUST produce output within 1 second of end-of-utterance.**

Options: acknowledge ("Got it"), paraphrase, confirm capture ("Noted"), signal thinking ("Let me check"), reflect note.

A 2-second silence feels broken. A 0.5-second "Got it, let me think" feels responsive.

Implementation: Realtime always runs fast path first. Even if routing to deep brain, the fast path produces acknowledgement before handoff.

## Voice Layer: Pipecat

**Pipecat handles:** VAD, barge-in, streaming TTS, endpointing, backpressure, cancellation.

**We build on top:** Ada interruption behavior, smart endpointing, noteboard streaming, multi-model routing, V2 awareness.

```
[Mic] → VAD → faster-whisper STT (RTX 4070)
  → ContextProcessor → IntentClassifier → BrainRouter
  → NoteboardStreamer → Kokoro TTS (CPU) → [Speaker]
```

## Project Structure

```
~/GitHub/AdaOS/
├── pyproject.toml
├── .env
├── PLAN.md
├── EXECUTION.md
├── TASK_SCHEMA.md
├── RUNTIME_STATE_MACHINE.md
├── TOOL_POLICY.md
├── ada/
│   ├── main.py                   # Entry point
│   ├── config.py                 # Settings, budgets, caps, model profiles (see below)
│   ├── state.py                  # Voice + Executive state machines
│   ├── pipeline/
│   │   ├── factory.py            # Pipecat pipeline assembly
│   │   └── frames.py            # Custom frames
│   ├── decision/                  # Layer 3 — the core of Ada
│   │   ├── event.py              # DecisionEvent dataclass
│   │   ├── classifier.py         # Intent classification (LLM + grammar)
│   │   ├── resolver.py           # Action resolution (pure code, no LLM)
│   │   ├── intent_schema.json     # JSON Schema for Ollama structured output
│   │   └── router.py             # Agent routing logic
│   ├── realtime/
│   │   ├── responder.py          # Response generation (second LLM call)
│   │   └── streamer.py           # Noteboard streaming
│   ├── executive/
│   │   ├── task_manager.py       # Task lifecycle
│   │   ├── dispatcher.py         # Agent dispatch with structured packets
│   │   ├── validator.py          # 4-gate validation
│   │   ├── retry_engine.py       # 3-tier retry, hard cap
│   │   ├── scheduler.py          # Date/condition triggers
│   │   ├── briefing.py           # Daily briefing compiler
│   │   ├── hooks.py              # Hook engine
│   │   └── health.py             # Health check loop
│   ├── brain/
│   │   ├── router.py             # Multi-model router
│   │   ├── aggregator.py         # Response merging
│   │   └── guardrails.py         # Structured output enforcement
│   ├── agents/
│   │   ├── openclaw_dispatcher.py # Spawns OpenClaw processes for Arch/Forge
│   │   ├── result_handler.py      # Reads workspace output, async results
│   │   ├── arch_soul.md           # Arch system prompt (read-only + spec output)
│   │   └── forge_soul.md          # Forge system prompt (repo write access)
│   ├── ideas/
│   │   ├── pipeline.py           # Idea → Brief → Spec → Impl → Done
│   │   └── models.py             # Data classes
│   ├── noteboard/
│   │   ├── server.py             # Bidirectional WebSocket
│   │   ├── renderer.py           # Note formatting
│   │   └── models.py             # Note data model
│   ├── ui/
│   │   └── noteboard.html        # Live canvas
│   ├── memory/
│   │   ├── store.py              # PostgreSQL connection + queries
│   │   ├── schema.sql            # Full schema (episodes, memories, entities, relations, tasks, notes, journal)
│   │   ├── consolidation.py      # Episodic → semantic extraction (background LLM process)
│   │   ├── context_builder.py    # Prompt assembly from all memory types
│   │   ├── compactor.py          # 3-layer compaction
│   │   └── models.py             # Task, Note, Session, Turn, Episode, Memory, Entity, Relation
│   ├── journal/
│   │   ├── logger.py             # Append-only action log
│   │   ├── budget_tracker.py     # Token spend tracking
│   │   └── queries.py            # Journal query interface
│   ├── tools/
│   │   ├── registry.py           # Category-based deferred loading
│   │   ├── core.py               # Tier 1 tools (13)
│   │   └── extended.py           # Tier 2 tools
│   ├── voice/
│   │   ├── wake.py               # Wake word / button
│   │   ├── interruption.py       # Barge-in
│   │   └── feedback.py           # Audio chimes
│   ├── ultraplan/                 # Overnight batch planning (ULTRAPLAN-inspired)
│   │   ├── __init__.py            # Module doc + architecture overview
│   │   ├── daemon.py              # Overnight loop, GPU management
│   │   ├── planner.py             # Multi-pass: decompose → critique → synthesize
│   │   ├── queue.py               # PostgreSQL task queue
│   │   ├── templates.py           # Planning prompts + Aristotle preamble
│   │   ├── reviewer.py            # Morning review (voice + noteboard)
│   │   └── config.py              # Model, GPU, timing settings
│   └── awareness/                # V2
│       ├── entity_tracker.py
│       ├── conversation_detector.py
│       ├── insertion_engine.py
│       └── scene.py
├── data/
│   └── sounds/
├── systemd/
│   └── adaos.service
└── scripts/
    ├── install.sh
    └── test_audio.py
```

## V2: Sound Awareness Engine

Replace wake word with intelligent audio scene understanding.

**Sound Entity Tracker:** classify sources (user, TV, guests, ambient) with states and priorities.

**Conversation Detector:**
| Scenario | Ada's Behavior |
|----------|---------------|
| User talking to Ada | Full attention, respond |
| Follow-up (<5 min) | Stay attentive |
| User talking to another person | Listen passively |
| TV/media | Ambient context |
| Silence after conversation | Decay to passive |

**Insertion Engine:** SPEAK_NOW / HOLD / DELAY / EXPIRE / DOWNGRADE

**V2 Phases:** V2.1 diarization → V2.2 audio classification → V2.3 conversation state machine → V2.4 insertion engine → V2.5 multi-device audio

## Key Decisions

- **Ada is an operator, not an assistant** — high trust, aggressive execution, rollback-capable
- **Escalate for direction, irreversibility, budget** — everything else is autonomous
- **3 agents (Ada, Arch, Forge), strict hierarchy** — Ada is sole coordination authority
- **Two harnesses** — claw-code-parity (Ada, local, GPT-OSS 20B) + OpenClaw (Arch/Forge, cloud, DeepSeek V3.2)
- **4 internal subsystems** — Realtime, Executive, Memory, Journal
- **Unified Task object** — backbone connecting everything
- **Voice → Note → Task → Dispatch** — never dispatch directly from messy speech
- **Note states** — captured → pinned → candidate_task → promoted_to_task → archived
- **4-gate validation, 3-tier retry** — structured, capped, never infinite
- **3 policy bands** — free autonomous, autonomous with logging, escalate once
- **3 tool tiers** — always exposed (13), conditionally loaded, specialist-only
- **Execution journal** — observability replaces permissions
- **Structured output over prompt trust** — grammar-enforced, code enforces
- **1-second acknowledgement rule** — perceived latency is king (~250-350ms actual with MoE speed)
- **GPT-OSS 20B** — MoE (3.6B active, 20.9B total), 140 tok/s, native tool calling, 14GB VRAM
- **claw-code-parity** — Rust agent harness, full Claude Code tool surface, MIT, compiled binary
- **PostgreSQL 16 + pgvector + TimescaleDB** — unified memory: episodic, semantic, relational, structured. One ACID transaction.
- **Memory consolidation** — background LLM extracts facts from episodes (hippocampal replay pattern, Mem0-style)
- **Temporal knowledge** — memories have valid_from/valid_until, handles contradictions over time
- **Local tokens for volume, cloud for quality** — Ada free 24/7, Arch/Forge use DeepSeek V3.2 ($100/month budget, ~333M tokens)
- **Pipecat** framework, **PostgreSQL** memory, **Ollama** serves LLMs, **Kokoro** TTS on CPU

## Verification

1. Phase 1: speak → hear response (<1s to first audio)
2. Phase 2: restart → memory persists, task objects survive, context rebuilds
3. Phase 3: noteboard reflects live conversation + note states + task status
4. Phase 4: simple vs complex → different models, nvidia-smi confirms
5. Phase 5: voice → note → task → dispatch → validate → deliver (full pipeline)
6. Phase 6: tool loading changes based on intent category
7. Phase 7: wake word → conversation → idle → restart → resume pending tasks
8. Phase 8: agent delivers broken code → auto-retry → delivers only passing work
9. Phase 9: kill Ollama → DEGRADED, restore → recovery. Query journal → see everything Ada did.
10. V2: TV on → Ada ignores. Guest → quiet. Direct address → responds.
