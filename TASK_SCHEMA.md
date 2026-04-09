# AdaOS: Task Schema

## Purpose

The Task is the central data object in AdaOS. Every piece of executable work — from a brainstorm capture to a full implementation cycle — flows through a unified Task. The noteboard, agent dispatch, validation, scheduling, retries, and summaries are all views on the Task store.

## Base Schema

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
  "brief": "Short structured description of the work",
  "success_criteria": [
    "criterion 1",
    "criterion 2"
  ],
  "constraints": [
    "constraint 1"
  ],
  "artifacts_expected": [
    "spec",
    "code",
    "summary"
  ],
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

## Field Definitions

### Identity

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | Unique ID. Format: `tsk_YYYYMMDD_NNN` |
| `title` | string | Human-readable short description |

### Classification

| Field | Type | Description |
|-------|------|-------------|
| `type` | enum | What kind of work this is |
| `origin` | enum | How the task was created |
| `priority` | enum | Execution priority |
| `visibility` | enum | Who can see this task |
| `budget_class` | enum | Expected cost tier |

**Type values:**
- `architecture` — design decisions, tradeoff analysis, system planning
- `implementation` — code writing, prototyping, building
- `research` — scanning, summarizing, investigating
- `validation` — testing, reviewing, checking outputs
- `note_followup` — promoted from a note that became actionable
- `system` — internal Ada operations (compaction, health checks, briefings)

**Origin values:**
- `voice` — created from conversation
- `noteboard` — promoted from noteboard note
- `scheduled` — triggered by time/date condition
- `conditional` — triggered by event ("when X happens, do Y")
- `agent_followup` — spawned by agent result (e.g., Arch recommends Forge implement)
- `manual` — created directly via tool call

**Visibility values:**
- `personal` — Daniel only, never shared with agents
- `ada_private` — Ada can see and act on it, but does not expose to agents unless promoted
- `team_shareable` — can be dispatched to agents

### Ownership

| Field | Type | Description |
|-------|------|-------------|
| `owner` | enum | Who currently owns execution of this task |
| `created_by` | enum | Who originally created it |
| `dispatch_target` | string/null | Which agent this is dispatched to (null if not dispatched) |

Ownership transfers during lifecycle:
- Created by ADA from voice → owner: ADA
- Dispatched to Arch → owner: ARCH
- Result returns, validating → owner: ADA
- Needs user decision → owner: DANIEL
- User decides, sent to Forge → owner: FORGE

### Content

| Field | Type | Description |
|-------|------|-------------|
| `brief` | string | Structured description of the work. Written by Ada Executive from raw voice input. |
| `success_criteria` | string[] | What "done" looks like. Must be defined before dispatch. |
| `constraints` | string[] | Limits, requirements, non-negotiables. |
| `artifacts_expected` | string[] | What the agent should return (spec, code, summary, etc.) |
| `artifacts_received` | object[] | What actually came back. Each entry: `{type, content, agent, timestamp}` |

### Relationships

| Field | Type | Description |
|-------|------|-------------|
| `linked_notes` | string[] | Note IDs connected to this task |
| `linked_tasks` | string[] | Other task IDs (dependencies, follow-ups, parent tasks) |

### Execution Control

| Field | Type | Description |
|-------|------|-------------|
| `retry_count` | int | How many retries have been attempted |
| `max_retries` | int | Hard cap. Default: 3. |
| `cloud_budget_limit` | int | Max cloud tokens for this task. 0 = use default from config. |
| `requires_user_decision` | bool | If true, task is blocked until Daniel responds |
| `escalation_reason` | string/null | Why this was escalated (null if not escalated) |

### Timestamps

| Field | Type | Description |
|-------|------|-------------|
| `created_at` | ISO 8601 | When the task was created |
| `updated_at` | ISO 8601 | Last modification |
| `completed_at` | ISO 8601/null | When status reached `done`, `failed`, or `cancelled` |

## Status Lifecycle

```
draft → queued → dispatched → in_progress → awaiting_result → validating → done
                                                                    ↓
                                                              (retry) → dispatched
                                                                    ↓
                                                              (max retries) → failed
                                                                    ↓
                                                              (needs decision) → awaiting_user → dispatched
                                                                    ↓
                                                              (user cancels) → cancelled
```

**Status definitions:**

| Status | Meaning |
|--------|---------|
| `draft` | Captured from voice/note but not yet structured enough to act on |
| `queued` | Brief is written, success criteria defined, ready for dispatch |
| `dispatched` | Sent to an agent, waiting for them to start |
| `in_progress` | Agent is actively working (if trackable) |
| `awaiting_result` | Agent work complete, result in transit |
| `awaiting_user` | Blocked on Daniel's decision |
| `validating` | Ada Executive running validation gates |
| `done` | All gates passed, result delivered or stored |
| `failed` | Max retries exhausted, or unrecoverable error |
| `cancelled` | User or Ada cancelled the task |

## The Path: Voice → Note → Task → Dispatch

A task should NOT dispatch directly from messy speech. The flow is:

```
1. Voice input → Ada Realtime captures intent
2. Intent → Note (captured state)
3. Note evaluation:
   - stays a note (thought, reminder, reflection, half-baked idea)
   - OR promoted to task (has actionable intent)
4. Task creation → Ada Executive structures brief + criteria
5. Dispatch evaluation:
   - Ada handles locally via claw-code-parity (storage, search, formatting, light validation, simple research)
   - OR dispatch to ARCH via OpenClaw (architecture, design, tradeoffs, deep research)
   - OR dispatch to FORGE via OpenClaw (implementation, code, repo mutation)
6. Agent works → result returns → validation → delivery or retry
```

### What makes a note become a task?

A note becomes a task when it implies executable commitment. Not all notes should become tasks.

**Stays a note if it is:**
- A thought or reflection
- A reminder without a clear next step
- A half-baked idea
- Background context
- Something you want saved, not acted on

**Becomes a task if it has:**
- Intent to act
- Clear enough object of work
- Owner or target implied
- Implied next step
- Time sensitivity or execution request

**Promotion triggers:**
1. **Explicit command:** "send this to Arch", "prototype this", "look into this"
2. **Clear actionable structure:** "We should compare Rust and Go for the backend this week"
3. **Ada asks, user confirms:** "Should I turn that into a task?"

**Note states:**
```
captured → pinned → candidate_task → promoted_to_task → archived
```

### What makes a task become a dispatch?

A task dispatches when:
- It has a clear owner outside Ada
- The brief is structured enough
- Success criteria are minimally defined
- Budget/risk rules allow it
- No blocking user decision remains

**Dispatch routing:**
- **Ada local** (claw-code-parity + GPT-OSS 20B): storage, summarization, note handling, light validation, search, formatting, simple research
- **ARCH** (OpenClaw + Claude Opus): architecture, tradeoffs, system design, planning under uncertainty, deep research
- **FORGE** (OpenClaw + Claude Opus): concrete implementation, repo mutation, code writing, patch generation

## Structured Packets

### Ada → Agent (Task Packet)

When dispatching, Ada sends a structured packet — never free-form chat:

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

### Agent → Ada (Result Packet)

Agents return structured results — never chat:

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

## Validation Gates (applied to results)

A result is deliverable if it passes 4 gates, in order:

### Gate 1 — Structural Completeness
Did the agent return what Ada asked for?
- Spec returned when spec requested
- Code returned when implementation requested
- Research summary returned when scan requested
- Required fields present, format correct
- Automated, deterministic, no LLM needed.

### Gate 2 — Technical Validity
For code/system tasks:
- Builds or runs
- Tests pass if relevant
- No obvious broken imports/dependencies
- Output is inspectable

For research tasks:
- Coherent
- Sourced or internally traceable
- Not obviously malformed

Automated, runs sandboxed commands.

### Gate 3 — Intent Alignment
The result must match:
- Your original idea
- The latest pivot (not an old direction)
- The selected path

A technically correct answer to the wrong problem is not deliverable.
LLM-assisted (Ada evaluates with context of original task brief).

### Gate 4 — Attention-Worthiness
Even a valid result should only interrupt you if:
- It is done
- It needs your decision
- It is blocked
- It changes direction
- It is worth surfacing now

Otherwise Ada holds it, stores it, or summarizes later.

**Delivery classes:**

| Class | When | Examples |
|-------|------|---------|
| Immediate delivery | Needs attention now | Decision needed, finished implementation, critical blocker, budget issue, architecture fork |
| Delayed delivery | Useful but not urgent | Background research, progress update, noncritical result |
| Silent storage | Not worth surfacing | Raw logs, low-value intermediate output, redundant progress traces |

## Retry Policy

Hard caps. No infinite loops.

```
Retry 1 — Automatic local retry
  Same agent, same task, specific error feedback appended.
  "Tests failed on line 42: TypeError. Fix and resubmit."

Retry 2 — Reformulated retry
  Ada rewrites the brief with more context/constraints.
  "Original brief was ambiguous about X. Clarified: Y."

Retry 3 — Escalated retry with diagnostics
  Full error chain, all previous attempts, explicit failure analysis.
  "Three approaches tried. All fail because Z. Need different strategy."

After 3 retries → STOP.
  Ada reports to user: "Forge couldn't solve this after 3 attempts.
  Here's what was tried. Want me to escalate to Arch for a different approach?"
```

## PostgreSQL Storage

Tasks stored in PostgreSQL (same database as episodes, memories, journal). Full schema in PLAN.md `memory/schema.sql`.

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    priority TEXT NOT NULL DEFAULT 'medium',
    visibility TEXT NOT NULL DEFAULT 'ada_private',
    owner TEXT NOT NULL DEFAULT 'ADA',
    created_by TEXT NOT NULL,
    brief TEXT,
    success_criteria JSONB,
    constraints JSONB,
    artifacts_expected JSONB,
    artifacts_received JSONB,
    linked_notes JSONB,
    linked_tasks JSONB,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    budget_class TEXT DEFAULT 'free',
    cloud_budget_limit INTEGER DEFAULT 0,
    requires_user_decision BOOLEAN DEFAULT FALSE,
    escalation_reason TEXT,
    dispatch_target TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_owner ON tasks(owner);
CREATE INDEX idx_tasks_priority ON tasks(priority);
```
