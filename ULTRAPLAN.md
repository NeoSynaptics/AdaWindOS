# UltraPlan (Overnight Planning)

UltraPlan is Ada's deep planning subsystem. It runs overnight when GPUs are free, using a 72B parameter model for multi-pass planning that would be too slow and expensive during daytime interaction.

Inspired by Anthropic's ULTRAPLAN concept, but fully local — no cloud cost, multi-pass self-critique, 8-hour budget, dual GPU utilization.

## How It Works

```
User: "Plan the SportWave sensor fusion architecture overnight"
  ↓
Ada queues a PlanTask → ultraplan_queue table
  ↓
23:00: UltraPlan daemon wakes up
  ↓
APU unloads voice model, loads Qwen 72B across both GPUs
  ↓
Pass 1: Decompose (break problem into components)
Pass 2: Critique (challenge every assumption, find gaps)
Pass 3: Synthesize (rebuild plan incorporating critique)
  ↓
Result persisted to database
  ↓
07:00: APU restores voice model
  ↓
Ada's morning greeting: "I planned 3 tasks overnight. Here's what I found..."
```

## Three-Pass Planning

### Pass 1: Decompose

Breaks the problem into structured components:

1. **Problem statement** — precise restatement
2. **Assumptions** — listed with validation status (validated / reasonable / risky)
3. **Components** — discrete pieces of work with dependencies, risks, complexity estimates
4. **Dependencies** — directed graph, critical path identified
5. **Unknowns** — what experiments would reduce uncertainty
6. **Draft sequence** — ordered steps with parallelization markers

### Pass 2: Critique

Adversarial review of the decomposition:

1. **Assumption challenges** — which assumptions are wrong?
2. **Missing components** — what was left out or oversimplified?
3. **Dependency risks** — where could the critical path break?
4. **Complexity underestimates** — what's harder than it looks?
5. **Alternative approaches** — is there a fundamentally better way?
6. **Showstoppers** — anything that makes the plan infeasible?

### Pass 3: Synthesize

Final plan incorporating critique feedback:

- Summary, revised components, execution sequence
- Key decisions and tradeoffs documented
- Open questions requiring human input
- Validation criteria for correctness

### Aristotle Mode (Optional)

A first-principles preamble can be prepended to Pass 1 for tasks that need clean-sheet thinking:

1. What is the ACTUAL problem, stripped of conventional framing?
2. What do we KNOW to be true (proven, not assumed)?
3. Which constraints are REAL (physics, hardware) vs ARTIFICIAL (habit)?
4. Starting from only proven truths, what is the simplest solution?
5. Then layer on practical considerations.

Auto-triggered when: domain is new, brief contains "from scratch" / "rethink", or task type is architecture.

## Task Lifecycle

```
submitted → claimed → planning → completed → reviewed
                         ↓
                       failed → retry (up to 2 retries)
```

| Status | Meaning |
|--------|---------|
| `submitted` | Queued by user or Ada |
| `claimed` | Daemon picked it up |
| `planning` | Active generation (passes in progress) |
| `completed` | All passes done, plan ready for review |
| `reviewed` | User saw it in morning briefing |
| `failed` | Planning failed after max retries |
| `cancelled` | User cancelled before execution |

## Domain Context

Plans are informed by domain-specific context overlays:

| Domain | Context Provided |
|--------|-----------------|
| `sportwave` | Hardware (LD2451), stack (Expo, FastAPI, Tauri), constraints (real-time, sensor fusion) |
| `blackdirt` | Architecture (Group Brain + Couzin School), constraints (500+ dots @ 60fps) |
| `adaos` | Stack (Pipecat, Qwen, Ollama), constraints (<1s latency, local-first) |
| `legal` | Case context, constraints (legal accuracy, document cross-referencing) |
| `general` | No specific context |

## Configuration

All in `ada/ultraplan/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `planning_model` | `qwen3:72b` | Primary planning model |
| `fallback_model` | `qwen3:14b` | If 72B fails to load |
| `overnight_start_hour` | 23 | Daemon activates |
| `overnight_end_hour` | 7 | Daemon yields GPUs |
| `max_passes` | 3 | decompose → critique → synthesize |
| `max_minutes_per_task` | 60 | Hard cap per planning task |
| `max_tasks_per_night` | 8 | Realistic cap (~50 min per task) |
| `max_queued_tasks` | 20 | Queue size limit |
| `cooldown_between_tasks_sec` | 30 | Pause between tasks |

## Components

### UltraPlanDaemon (`ada/ultraplan/daemon.py`)

Background process that:

1. Waits until overnight window (23:00-07:00)
2. Loads planning model via Ollama
3. Processes queue: claim → plan → persist → next
4. Releases GPUs at end of window
5. Records results in journal for Ada's morning briefing

Can run standalone: `python -m ada.ultraplan.daemon`

### UltraPlanner (`ada/ultraplan/planner.py`)

Orchestrates the three planning passes against Ollama. Each pass is a full LLM generation with intermediate results persisted to the queue.

**Status:** Stubbed with TODOs. Pass orchestration and Ollama integration not yet implemented.

### PlanQueue (`ada/ultraplan/queue.py`)

PostgreSQL-backed queue using the shared Store. Complete implementation:

- `submit()` — queue a task
- `get_pending()` — next task by priority
- `claim()` / `complete()` / `fail()` — lifecycle transitions
- `update_progress()` — persist intermediate pass outputs
- `get_completed_unreviewed()` — for morning briefing
- `cancel()` — cancel before execution

### Templates (`ada/ultraplan/templates.py`)

Prompt templates for each pass plus domain context overlays. Includes the Aristotle first-principles preamble.

## Database Table

```sql
ultraplan_queue (
    task_id TEXT PRIMARY KEY,
    title TEXT,
    brief TEXT,
    domain TEXT DEFAULT 'general',
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'submitted',
    linked_task_id TEXT,          -- link to Ada Executive task
    submitted_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    passes_completed INTEGER DEFAULT 0,
    final_plan TEXT,
    intermediate_outputs JSONB,  -- {pass_1: ..., pass_2: ..., pass_3: ...}
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2
)
```

## Integration with Ada

- **Queueing:** Ada's action resolver routes `queue_ultraplan` actions. User says "plan this overnight" → task lands in queue.
- **Morning briefing:** Ada calls `daemon.get_morning_briefing()` on first greeting after overnight window.
- **APU coordination:** The APU idle monitor triggers `transition_to_planning_mode()` during overnight hours.
- **Journal:** Completion is logged to journal for auditability.

## Implementation Status

- Queue (PostgreSQL CRUD): complete
- Daemon (overnight loop, model loading, GPU release): complete (structure)
- Templates (3-pass prompts, domain context, Aristotle): complete
- Config: complete
- Planner (pass orchestration, Ollama integration): **not implemented** (TODOs)
- Reviewer (pre-morning-briefing review pass): not implemented
- CLI commands (`ada plan queue/list/review`): not implemented
- Systemd unit file: not implemented
