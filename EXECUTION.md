# AdaOS: Execution Plan

## Gaps Fixed (all reviews consolidated)

1. Ada's system prompt must be written before Phase 1
2. Use Pipecat's built-in OllamaLLMService for fast brain (not custom API)
3. Arch/Forge dispatched via OpenClaw (subprocess spawn + filesystem workspace, DeepSeek V3.2 API)
4. Noteboard WebSocket is bidirectional (UI edits → Ada is notified)
5. Fallback strategy: Ada degrades gracefully (Ollama down → text-only, OpenClaw down → local-only)
6. Audio device selection must be tested before Phase 1
7. Ada split into 4 internal subsystems: Realtime, Executive, Memory, Journal
8. Task object defined and implemented before agent dispatch
9. Tool belt starts lean (13 Tier 1 tools), expanded when justified
10. Structured output schemas enforced via grammar, not just prompting
11. Two-layer state machine (Voice + Executive) defined before Phase 1 code
12. Validation is 4 gates, retry is 3 tiers with hard cap at 3
13. Budget/rate controls implemented alongside agent dispatch, not after
14. Team is 3 agents (Ada, Arch, Forge) — Sage removed for now
15. Operating doctrine: high-trust operator, not cautious assistant
16. Escalation: direction, irreversibility, budget boundary ONLY
17. Three policy bands: free autonomous, autonomous with logging, escalate once
18. Execution journal replaces permission gates — observability over approval
19. Note states: captured → pinned → candidate_task → promoted_to_task → archived
20. Voice → Note → Task → Dispatch flow (never dispatch from raw speech)
21. Delivery classes: immediate, delayed, silent storage
22. Proactive behaviors are core, not optional (interrupts, summaries, reminders)
23. The atom is the Decision Event, not the Frame — design Layer 3 first
24. LLM classifies intent; code resolves action (Action Resolver is deterministic Python)
25. Two-call pattern: classification (grammar-constrained) + response (free-form) — separate concerns
26. 15 intents, 16 actions — exhaustive, closed sets, no free-form
27. Decision Engine designed before Pipecat pipeline — Layer 3 before Layer 1
28. Voice brain: GPT-OSS 20B MoE (20.9B total, 3.6B active, ~140 tok/s, 14GB VRAM, native tool calling)
29. Agent harness: claw-code-parity (Rust, MIT, full Claude Code tool surface, Ollama via openai_compat)
30. Not OpenClaude (legal risk), not learn-claude-code (pedagogical only), not custom (months of work)
31. Memory: PostgreSQL 16 + pgvector + TimescaleDB (not SQLite — learning system needs real vector search + temporal partitioning)
32. Brain-inspired memory: episodic (TimescaleDB) + semantic (pgvector) + relational (recursive CTEs) + consolidation (LLM extraction)
33. Temporal knowledge: memories have valid_from/valid_until — handles contradictions over time
34. Local embedding model: BAAI/bge-small-en-v1.5 (384-dim, CPU, no API dependency)
35. JSON Schema via Ollama `format` parameter, not custom GBNF grammar
36. Configurable model profiles (FAST_MODEL, EMBED_MODEL, PLAN_MODEL, AGENT_MODEL) in config.py
37. Outbox pattern for restart-safe dispatch: write outbox event in same transaction as state change
38. Gate 2 sandbox: isolate test/build in bwrap/Docker before Forge gets autonomous repo write

## Reference Documents

- [PLAN.md](PLAN.md) — full system design
- [DECISION_ENGINE.md](DECISION_ENGINE.md) — the atom (Decision Event), intent/action taxonomy, Action Resolver, grammar, 20 traced conversations
- [TASK_SCHEMA.md](TASK_SCHEMA.md) — Task object, note promotion, validation gates, retry policy
- [RUNTIME_STATE_MACHINE.md](RUNTIME_STATE_MACHINE.md) — states, transitions, concurrency, health checks
- [TOOL_POLICY.md](TOOL_POLICY.md) — policy bands, tool tiers, budget controls, decision gates, journal

## Execution Order

### Phase 0: Foundation (before any code)

**Prompt & Identity:**
- [ ] Write Ada's system prompt (~/.ada/instructions.md)
- [ ] Write Ada's personality definition (warm to Daniel, strict to agents)
- [ ] Write operating doctrine into system prompt (high-trust operator, not cautious assistant)
- [ ] Write tool-use instructions (Tier 1 tools, when to dispatch, when to handle locally)
- [ ] Define two-layer state machine: Voice FSM + Executive FSM + overlay states

**Decision Engine (Layer 3 — design before code):**
- [ ] Write intent_schema.json (JSON Schema for Ollama structured output)
- [ ] Write classification prompt (~800 tokens, used every turn)
- [ ] Write response generation prompt (separate from classification)
- [ ] Write Action Resolver rules in Python (resolver.py — pure code, no LLM)
- [ ] Write agent routing logic (router.py — keyword-based deterministic routing)
- [ ] Define all 15 intents with examples
- [ ] Define all 16 actions with band assignments
- [ ] Test grammar on GPT-OSS 20B via Ollama (valid JSON every time?)
- [ ] Test two-call latency: classification (~50-80ms) + response (~100-150ms) = <350ms?
- [ ] Run 20 conversations mentally through the Decision Event model

**Infrastructure Setup (Linux PC):**
- [ ] Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
- [ ] Pull model: `ollama pull gpt-oss:20b` (~14GB, fits RTX 5060 Ti)
- [ ] Verify model: `ollama run gpt-oss:20b "Hello"` — confirm ~140 tok/s
- [ ] Test structured output: `ollama run gpt-oss:20b --format json` — valid JSON?
- [ ] Test native tool calling: send tool definitions, verify model returns structured tool-call JSON
- [ ] Clone claw-code-parity: `git clone https://github.com/ultraworkers/claw-code-parity.git`
- [ ] Build harness: `cd claw-code-parity/rust && cargo build --release`
- [ ] Configure harness for Ollama: `OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1`
- [ ] Test harness: `./target/release/claw --model gpt-oss:20b` — verify tool calling works end-to-end
- [ ] Wire env vars into shell profile (~/.bashrc or ~/.zshrc)

**OpenClaw + DeepSeek V3.2 (Arch/Forge harness):**
- [ ] Install OpenClaw: `npm install -g openclaw` (or clone openclaw/openclaw repo + build)
- [ ] Get DeepSeek API key: https://platform.deepseek.com/
- [ ] Configure `~/.openclaw/openclaw.json`:
  ```json
  {
    "models": {
      "mode": "merge",
      "providers": {
        "deepseek": {
          "baseUrl": "https://api.deepseek.com/v1",
          "apiKey": "${DEEPSEEK_API_KEY}",
          "api": "openai-completions",
          "models": [
            {"id": "deepseek-chat", "name": "DeepSeek V3.2", "contextTokens": 163840}
          ]
        }
      }
    },
    "agents": {
      "defaults": {
        "model": {"primary": "deepseek/deepseek-chat"}
      }
    }
  }
  ```
- [ ] Export key: `export DEEPSEEK_API_KEY=sk-...`
- [ ] Test: `openclaw --prompt "Hello, list files in current directory"` — verify DeepSeek V3.2 + tool calling works
- [ ] Write Arch soul file: `.openclaw/agents/arch/SOUL.md` (system designer, read-only, spec output)
- [ ] Write Forge soul file: `.openclaw/agents/forge/SOUL.md` (code author, repo write access)
- [ ] Test dispatch pattern: spawn OpenClaw with task_packet.json, verify workspace output
- [ ] Set up OpenRouter as fallback provider (in case DeepSeek has extended outage)

**PostgreSQL + pgvector + TimescaleDB:**
- [ ] Install Docker: `sudo apt install docker.io docker-compose`
- [ ] Create docker-compose.yml with `timescale/timescaledb-ha:pg16` image
- [ ] `docker-compose up -d` — PostgreSQL running with pgvector + TimescaleDB
- [ ] Connect: `psql -h localhost -U ada -d ada`
- [ ] Enable extensions: `CREATE EXTENSION vector; CREATE EXTENSION timescaledb;`
- [ ] Run schema.sql — create episodes (hypertable), memories, entities, relations, tasks, notes, journal tables
- [ ] Verify: insert test episode with embedding, query with vector similarity
- [ ] Install Python driver: `pip install asyncpg` (async PostgreSQL)

**Hardware & Audio Testing:**
- [ ] Test audio hardware (mic + speaker on Linux PC)
- [ ] Test faster-whisper on RTX 4070 (latency, accuracy)
- [ ] Test Kokoro TTS on CPU (latency, quality)
- [ ] Verify GPU isolation: GPT-OSS 20B on 5060 Ti (~14GB), Whisper on 4070 (~2GB), no contention
- [ ] Run nvidia-smi during simultaneous inference — confirm both GPUs active, no spillover

**Python Environment:**
- [ ] Install Python deps in ~/venvs/voice
- [ ] Set up project structure (dirs only, no code yet)

**Files:** ~/.ada/instructions.md, scripts/test_audio.py, scripts/setup_linux.sh, pyproject.toml
**Verify:** Ollama serves GPT-OSS 20B at ~140 tok/s. claw-code-parity runs and executes tool calls via Ollama. Structured JSON output valid 9/10 times. Audio hardware works. Both GPUs confirmed isolated.

### Phase 1: Voice Loop (speak → hear response)

**Goal:** Talk to Ada, she responds. Nothing else. Under 1 second to first audio.

- [ ] Voice state machine (state.py): IDLE → ATTENTIVE → LISTENING → PROCESSING_FAST → SPEAKING
- [ ] Executive state machine (state.py): starts at IDLE, no tasks yet
- [ ] Pipecat pipeline: Mic → VAD → Whisper STT → GPT-OSS 20B (via Ollama OllamaLLMService) → Kokoro TTS → Speaker
- [ ] System prompt loaded from ~/.ada/instructions.md
- [ ] Barge-in working (interrupt Ada → cancel TTS + LLM)
- [ ] Streaming TTS (starts speaking before full response generated)
- [ ] GPU pinning verified (Whisper on 4070, GPT-OSS 20B on 5060 Ti)
- [ ] 1-second acknowledgement rule enforced
- [ ] Latency measurement (target: <1s to first audio)

**Files:** main.py, config.py, state.py, pipeline/factory.py, pipeline/frames.py
**Test:** 5-minute conversation. Interrupt Ada. Ask questions. Verify <1s latency. Verify state transitions in logs.

### Phase 2: Memory Architecture + Data Models + Journal

**Goal:** Ada remembers across sessions. PostgreSQL stores all memory types. Task/Note/Episode/Memory data models exist. Execution journal captures actions. Context assembles from all memory types.

**PostgreSQL schema (implement first):**
- [ ] schema.sql: full schema from PLAN.md (episodes hypertable, memories, entities, relations, tasks, notes, journal)
- [ ] Episodes table as TimescaleDB hypertable (auto-partitioned by timestamp)
- [ ] Memories table with pgvector embeddings (384-dim, HNSW index)
- [ ] Entities + Relations tables (lightweight graph)
- [ ] Tasks, Notes, Journal tables (from TASK_SCHEMA.md, migrated from SQLite design)
- [ ] Python models in memory/models.py: Task, Note, Session, Turn, Episode, Memory, Entity, Relation, JournalEntry

**Data models:**
- [ ] Task model (full schema from TASK_SCHEMA.md)
- [ ] Note model (with states: captured/pinned/candidate_task/promoted_to_task/archived)
- [ ] Episode model (timestamped, with embedding, linked to decision_event)
- [ ] Memory model (extracted facts, with confidence, temporal validity, embedding)
- [ ] Entity + Relation models (for graph)

**Memory layer (PostgreSQL via asyncpg):**
- [ ] store.py: async PostgreSQL connection pool + query methods
- [ ] Episodic writes: every conversation turn → episodes table with embedding
- [ ] Semantic reads: pgvector similarity search for relevant memories
- [ ] Temporal queries: "what did we discuss last week?" via TimescaleDB time range
- [ ] Graph queries: recursive CTEs for entity-relationship traversal
- [ ] Context builder: system prompt + instruction files + recent episodes + relevant memories + active tasks + related entities
- [ ] Instruction file loading: walk directory tree for .ada/instructions.md
- [ ] Instruction file rules enforced: <500 global, <1K project, <500 agent, <1.5K max, dedup

**Memory consolidation (background process):**
- [ ] consolidation.py: runs every 30 minutes or after conversation ends
- [ ] Read unconsolidated episodes
- [ ] LLM extracts: facts, preferences, decisions, patterns, corrections
- [ ] Compare against existing memories (pgvector similarity — is this already known?)
- [ ] Write new memories or update existing (ADD / UPDATE / NOOP)
- [ ] Mark episodes as consolidated
- [ ] Update entity-relationship graph if new entities/relations discovered
- [ ] Temporal knowledge: set valid_until on superseded memories

**Execution journal:**
- [ ] Append-only journal table in PostgreSQL
- [ ] Journal entry: timestamp, action_type, summary, task_id, band, budget_impact, rollback_hint
- [ ] Budget tracker: running token totals, daily/weekly (SQL aggregation)
- [ ] Query interface: "what did I do today?", "how much spent?", "what's pending?"

**Compaction:**
- [ ] Micro-compaction: replace old tool results with summaries each turn
- [ ] Auto-compaction: compress at 50K token threshold, save transcript to episodes
- [ ] Summary chaining: multiple compactions merge instead of replace

**Embedding generation:**
- [ ] Local embedding model: BAAI/bge-small-en-v1.5 (384-dim, runs on CPU, fast)
- [ ] Embed every episode on write
- [ ] Embed every extracted memory on consolidation
- [ ] Embed search queries for similarity retrieval

**Files:** memory/store.py, memory/schema.sql, memory/consolidation.py, memory/context_builder.py, memory/compactor.py, memory/models.py, journal/logger.py, journal/budget_tracker.py, journal/queries.py
**Test:** Talk, restart Ada, "what were we talking about?" — remembers (episodes + memories). Create task via code — persists. "What did we decide about analytics?" — pgvector finds relevant memory. Journal entries appear for all actions. Consolidation extracts "Daniel prefers streaming pipeline" from conversation. Temporal query: "what happened yesterday?" returns correct episodes. Budget tracker shows 0 cloud tokens used.

### Phase 3: Noteboard (live canvas)

**Goal:** See ideas forming on screen as you talk. Notes have states. Notes link to tasks when promoted.

- [ ] WebSocket server (bidirectional: Ada → UI, UI edits → Ada)
- [ ] Note renderer: conversation → structured bullet points
- [ ] Note state machine: captured → pinned → candidate_task → promoted_to_task → archived
- [ ] Note classification: personal / actionable / scheduled / conditional
- [ ] Actionable notes auto-create Task objects (promotion trigger)
- [ ] Task status reflected on noteboard (LIVE, PINNED, TASKS, SCHEDULED sections)
- [ ] Noteboard HTML UI (localhost, vanilla JS + WebSocket)
- [ ] Notes persist in PostgreSQL across sessions
- [ ] Pin, edit, delete from UI → Ada notified
- [ ] Visibility enforcement: personal notes never in agent context

**Files:** noteboard/server.py, noteboard/renderer.py, noteboard/models.py, ui/noteboard.html, realtime/streamer.py
**Test:** Talk → notes appear live. "Just save this" → personal note (captured). "Send to Arch" → note promotes to task, visible in TASKS section. Edit in UI → Ada acknowledges. Restart → persists. Journal logs all note operations.

### Phase 4: Decision Engine + Multi-Model Router

**Goal:** Every input produces a Decision Event. Classification → Action Resolution → Execution. Grammar-enforced. Two-call pattern. Routes to right brain.

**Decision Engine (Layer 3):**
- [ ] DecisionEvent dataclass (event.py)
- [ ] Classifier: grammar-constrained LLM call, produces intent JSON (classifier.py)
- [ ] Action Resolver: deterministic Python, no LLM (resolver.py)
- [ ] Agent Router: keyword-based deterministic routing (router.py)
- [ ] JSON Schema loaded via Ollama `format` parameter
- [ ] Two-call pattern wired: classification → resolution → response generation
- [ ] Fallback chain: JSON parse → retry → keyword matching → ask user
- [ ] Budget gate: resolver checks budget before dispatch
- [ ] Band enforcement: resolver assigns policy band per action
- [ ] Chained actions: promote_note → create_task → dispatch_agent
- [ ] All 15 intents handled
- [ ] All 16 actions handled
- [ ] Every Decision Event logged to journal

**Model routing:**
- [ ] GPT-OSS 20B handles all local work (classification, response, local tool calls)
- [ ] Heavy work (architecture, complex code): dispatched to Arch/Forge via OpenClaw → Claude Opus
- [ ] Interim responses: "Let me check with Arch" while OpenClaw runs in background
- [ ] Parallel execution verified: Ada (GPU) + Arch/Forge (cloud) simultaneously

**Files:** decision/event.py, decision/classifier.py, decision/resolver.py, decision/router.py, decision/intent_schema.json, brain/router.py, brain/aggregator.py, brain/guardrails.py
**Test:** Trace 20 conversations from DECISION_ENGINE.md through live system. Verify: classification JSON valid every time, action resolver produces correct action, budget gate blocks over-limit, chained actions execute in order, journal captures every event. "What time is it?" → fast. "Analyze Rust vs Go" → deep. nvidia-smi confirms both paths. Malformed output → fallback chain works.

### Phase 5: Agent Dispatch + Idea Pipeline + Validation + Budget

**Goal:** Full Voice → Note → Task → Dispatch → Validate → Deliver pipeline. Budget controls active. Journal tracks everything.

**Agent dispatch (OpenClaw for Arch/Forge):**
- [ ] openclaw_dispatcher.py: spawns OpenClaw subprocess with task_packet.json + workspace dir
- [ ] Arch dispatch: OpenClaw + DeepSeek V3.2 + arch_soul.md (read-only, spec output)
- [ ] Forge dispatch: OpenClaw + DeepSeek V3.2 + forge_soul.md (repo write, code output)
- [ ] Structured task packets written to workspace (from TASK_SCHEMA.md)
- [ ] Result handler: reads workspace artifacts after OpenClaw process exits
- [ ] Background mode: task survives Ada restart, resurfacing logic on next session
- [ ] Workspace cleanup: archive completed workspaces, delete after 7 days

**Validation (4 gates):**
- [ ] Gate 1 — Structural: artifact type, format, fields (deterministic)
- [ ] Gate 2 — Technical: compile, test, command (sandboxed)
- [ ] Gate 3 — Intent alignment (LLM-assisted)
- [ ] Gate 4 — Attention-worthiness: immediate / delayed / silent storage

**Retry engine (3 tiers, hard cap):**
- [ ] Retry 1: automatic, specific error feedback
- [ ] Retry 2: reformulated brief
- [ ] Retry 3: escalated with full diagnostics
- [ ] After 3: STOP, report to Daniel

**Budget & rate controls:**
- [ ] Monthly budget cap: $100 (configurable)
- [ ] Daily soft cap: ~$3.30/day (~11M tokens at DeepSeek V3.2 rates)
- [ ] Per-task token cap (configurable, default 100K tokens)
- [ ] Max 3 retries per task (hard)
- [ ] Max 5 dispatches per hour (soft, warns)
- [ ] Max 10 concurrent background tasks
- [ ] 30s dispatch cooldown same agent
- [ ] Journal logs all spend with running totals
- [ ] Ada warns when approaching limits

**Idea pipeline:**
- [ ] Stages: idea → brief → spec → implementation → done
- [ ] Each stage transition creates/updates Task
- [ ] Reformulation: raw voice → clean agent-ready specs
- [ ] Tracking in PostgreSQL, linked to Tasks

**Decision gates:**
- [ ] Architecture fork → escalate
- [ ] Budget threshold → escalate
- [ ] Agent conflict → escalate
- [ ] Ambiguity after one clarification → escalate
- [ ] Confidence below threshold → escalate

**Outbox pattern (restart-safe dispatch):**
- [ ] outbox_events table in PostgreSQL (same schema as PLAN.md)
- [ ] Every dispatch/delivery writes outbox event in SAME transaction as task update
- [ ] Outbox worker: separate async loop, reads pending events, executes side effects
- [ ] On failure: increment attempt, backoff, retry (max 5 attempts)
- [ ] On Ada restart: worker picks up unprocessed outbox events automatically
- [ ] Idempotency: agent_run table tracks request_hash to prevent double-dispatch

**Policy band enforcement:**
- [ ] Band 1 actions: no confirmation needed
- [ ] Band 2 actions: journal logged with rollback hints
- [ ] Band 3 actions: escalate once, then proceed

**Files:** executive/task_manager.py, executive/dispatcher.py, executive/validator.py, executive/retry_engine.py, agents/openclaw_dispatcher.py, agents/result_handler.py, agents/arch_soul.md, agents/forge_soul.md, ideas/pipeline.py, ideas/models.py, tools/core.py
**Test:** Full pipeline: voice → note → "send to Arch" → note promotes → task created → dispatched (structured packet logged in journal) → result returns → 4-gate validation → delivered. Force failure → retry chain → stops at 3 → reports. Budget tracker shows token spend. Pending task survives restart.

### Phase 6: Tool Registry + Extended Tools

**Goal:** Tier 2 tools loaded conditionally based on intent. Tier 3 stays with specialists.

- [ ] Tool registry: maps intent categories → tool sets
- [ ] Tier 1 (13 tools) always loaded
- [ ] Tier 2 loaded on demand: schedule, task_create, spawn_subagent, edit_file, write_file, bash, etc.
- [ ] Tier 3 tools NOT in Ada's belt (specialist-only)
- [ ] Bash restricted mode: hard blocklist + soft blocklist (Band 3 confirmation)
- [ ] Tool descriptions include failure warnings
- [ ] Intent classifier output includes `tool_categories` field

**Files:** tools/registry.py, tools/core.py, tools/extended.py
**Test:** Question needing search → search tool present. File question → read_file present. No file question → file tools NOT in prompt. Bash blocklist enforced: `rm -rf /` rejected.

### Phase 7: Wake Word + 24/7

**Goal:** Ada runs as a daemon, always ready. Full state machine active.

- [ ] OpenWakeWord integration ("Hey Ada" or button)
- [ ] Full Voice FSM: IDLE → ATTENTIVE → LISTENING → ... → IDLE
- [ ] ATTENTIVE decay: 2min → IDLE
- [ ] Systemd service (adaos.service)
- [ ] Auto-restart on crash
- [ ] Structured logging (rotated)
- [ ] Startup resume: check pending background tasks, deliver results on greeting
- [ ] Proactive behaviors active: updates, steering asks, progress summaries, drift reminders

**Files:** voice/wake.py, voice/feedback.py, voice/interruption.py, systemd/adaos.service
**Test:** "Hey Ada" → works. Walk away → ATTENTIVE → IDLE (2min). Come back → works. Kill process → systemd restarts. Pending task before restart → reported on greeting. Ada proactively mentions overnight results.

### Phase 8: Hooks + Auto-Documentation + Quality Gate

**Goal:** Ada is proactive. Hooks fire automatically. Code quality enforced.

- [ ] hooks.yaml format (global + per-project)
- [ ] Pre-dispatch hook: reformulate messages
- [ ] Post-agent hook: auto-validate (triggers 4 gates)
- [ ] Post-conversation hook: summary, action items, noteboard update
- [ ] Auto-documentation: decisions log (.ada/decisions.md), changelogs
- [ ] Quality gate: Forge code must pass Gates 1-2 before reaching Daniel
- [ ] Gate 2 sandbox: isolate test/build execution (bwrap, Docker, or nsjail)
  - [ ] Read-only repo mount (or copy-on-write)
  - [ ] No credentials in sandbox environment
  - [ ] No network access
  - [ ] Resource limits (CPU, memory, timeout)
  - [ ] Must be in place BEFORE Forge gets autonomous repo write
- [ ] Daily briefing: compile overnight agent work

**Files:** executive/hooks.py, .ada/hooks.yaml
**Test:** Conversation ends → noteboard summary. Forge delivers broken code → auto-retry → fixed → delivered. .ada/decisions.md updated after completed pipeline. Daily briefing includes overnight work.

### Phase 9: Fallbacks + Health + Polish

**Goal:** Ada is reliable. Degrades gracefully. Reports her own status.

**Health check loop:**
- [ ] Every 60s: Ollama, OpenClaw, Whisper, Kokoro, PostgreSQL, disk, GPU
- [ ] Failure → DEGRADED overlay state, clear communication about limitations
- [ ] Recovery → exit DEGRADED, log recovery event

**Degraded modes:**
- [ ] Ollama down → "Brain offline, can still take notes." Note-taking only.
- [ ] OpenClaw/Claude down → local-only, queue agent tasks in PostgreSQL for later dispatch
- [ ] Whisper down → text-only mode
- [ ] Internet down → local-only, Ada local work still functions

**Monitoring:**
- [ ] "How are you?" → system status report
- [ ] "What are you working on?" → background task summary
- [ ] Token budget dashboard (daily/weekly from journal)
- [ ] Journal query: "What have you been doing?" → summarized action trace

**Policy finalization:**
- [ ] Band 1/2/3 permission model configured and tested
- [ ] Bash blocklist validated
- [ ] Budget caps validated against actual usage patterns

**Files:** executive/health.py, config.py (fallback configs), state.py (DEGRADED overlay)
**Test:** Kill Ollama → DEGRADED, note-taking works. Kill OpenClaw → tasks queue. Restore → tasks dispatch, DEGRADED exits. "How are you?" → status. "What have you been doing?" → journal summary. Budget query → accurate totals.

### Phase 10: UltraPlan — Overnight Batch Planning Daemon

**Goal:** Queue complex planning tasks during the day. Dual GPUs run deep multi-pass planning overnight. Wake up to 8 validated plans. Zero API cost.

**Inspired by:** Anthropic's ULTRAPLAN (leaked Claude Code feature — cloud Opus, 30-min budget, browser approval, `__ULTRAPLAN_TELEPORT_LOCAL__` sentinel). Our version: local, multi-pass, 8-hour budget, self-critiquing.

**Hardware strategy:**
- Daytime: GPU 0 (5060 Ti) = GPT-OSS 20B (Ada), GPU 1 (4070) = Whisper STT
- Overnight: both GPUs free → Qwen 72B Q4 across dual GPUs (~5-10 tok/s)
- ~50 min per plan × 8 plans = fits in one night

**Queue & lifecycle:**
- [ ] PostgreSQL queue table (ultraplan_queue) in ada database
- [ ] PlanTask data model: submitted → claimed → planning → completed → reviewed
- [ ] Submit from voice ("plan this overnight"), Ada Executive, or CLI
- [ ] Priority ordering (higher priority = planned first)
- [ ] Retry on failure (max 2 retries per task)

**Multi-pass planner:**
- [ ] Pass 1 — Decompose: components, assumptions, dependencies, unknowns, sequence
- [ ] Pass 2 — Critique: challenge assumptions, find gaps, identify risks, suggest alternatives
- [ ] Pass 3 — Synthesize: final plan incorporating critique, actionable, unambiguous
- [ ] Optional Aristotle preamble: first-principles mode (strip conventions, rebuild from axioms)
- [ ] Domain-specific context overlays (sportwave, blackdirt, adaos, legal, general)
- [ ] Intermediate outputs persisted (all 3 passes inspectable)

**Daemon loop:**
- [ ] Time-window gating (23:00 - 07:00 default, configurable)
- [ ] GPU availability check before starting (nvidia-smi, verify voice pipeline idle)
- [ ] Model pre-load (Qwen 72B via Ollama, fall back to GPT-OSS 20B if VRAM insufficient)
- [ ] Sequential task processing with cooldown between tasks
- [ ] GPU release at end of window (unload model, yield back to voice pipeline)
- [ ] Graceful shutdown mid-plan (persist progress, resume next night)
- [ ] Heartbeat for Ada health monitor

**Morning review:**
- [ ] Ada morning greeting includes plan summary ("6 plans overnight, 4 solid, 2 need input")
- [ ] Voice review: "tell me about plan 3" → Ada reads summary
- [ ] Noteboard cards: completed plans as reviewable items
- [ ] Approve → optionally promote to Ada Executive task for agent dispatch
- [ ] Refine → re-queue with user feedback, re-plan next night
- [ ] Reject → cancel with reason

**Integration with Ada Executive:**
- [ ] Ada detects complex tasks → suggests "want me to plan this overnight?"
- [ ] Approved plans create Executive tasks linked to the plan
- [ ] Plan outputs feed directly into agent dispatch briefs (Arch/Forge specs)
- [ ] Journal logs all planning activity and token usage

**Files:** ada/ultraplan/__init__.py, daemon.py, planner.py, queue.py, templates.py, reviewer.py, config.py
**Test:** Queue 3 tasks before bed → daemon runs → morning: Ada says "3 plans ready" → review one → approve → Executive task created → dispatched to Arch. Force model load failure → falls back to 14B. Kill daemon mid-plan → progress persisted → resumes next night.

### V2: Sound Awareness Engine (separate milestone)
- V2.1: Speaker diarization
- V2.2: Audio classification
- V2.3: Conversation state machine (replace wake word)
- V2.4: Insertion engine
- V2.5: Multi-device audio

## End-to-End Flow (when everything works)

```
1. You walk up to your PC
2. "Hey Ada" → wake word (IDLE → ATTENTIVE → LISTENING)
3. "I've been thinking about adding real-time analytics to SportWave"
4. Ada Realtime: (LISTENING → PROCESSING_FAST, <1s)
   Intent: {intent: "brainstorm", requires_agent: false, speak_now: true, note_action: "create"}
   [speaks] "Interesting. Let me capture that."
   [noteboard] • Idea: real-time analytics for SportWave (note status: captured)
   [journal] action: note_create, band: 1
5. You: "We'd need mmWave data streaming at 60fps minimum"
   [noteboard] note updated, detail appended
   [journal] action: note_update, band: 1
6. You: "What do you think, is this feasible?"
7. Ada Realtime: Intent: {intent: "dispatch", target: "ARCH", urgency: "medium"}
   [speaks] "Good architecture question. Let me check with Arch." (<1s)
   Note promoted: captured → promoted_to_task
   Task created: tsk_20260402_001, type: architecture, status: queued
   Ada Executive: structures brief, dispatches to Arch via OpenClaw
   Task: status → dispatched → awaiting_result, owner: ARCH
   Executive state: DISPATCHING → WAITING_BACKGROUND
   [journal] action: dispatch, agent: ARCH, band: 2, budget: normal, estimated: 5K tokens
8. You: "Also remind me to call the sensor supplier Friday"
   [noteboard] • Scheduled: call sensor supplier (2026-04-04)
   Task: tsk_20260402_002, type: system, origin: voice, status: queued
   [journal] action: task_create, band: 1
9. Ada: "Noted. Anything else?"
10. You: "No that's it for now"
11. Ada: "Got it. I'll let you know when Arch responds."
    Voice: SPEAKING → ATTENTIVE → (2min) → IDLE
    Executive: WAITING_BACKGROUND (still monitoring)
12. ... 2 minutes later ...
13. Arch responds (structured result packet, confidence: high)
14. Ada Executive validates:
    Gate 1 (structural): spec document present, format correct ✓
    Gate 2 (technical): N/A for spec ✓
    Gate 3 (intent): matches feasibility question ✓
    Gate 4 (attention): complete, user idle → delayed delivery (next session)
    Task: status → validating → done (pending delivery)
    [journal] action: validate, gates: [pass, pass, pass, delay], band: 2
15. Next "Hey Ada":
    Ada: "Welcome back. Arch came back on the analytics idea.
    Feasible at 30fps, recommends scaling up later.
    Two approaches: streaming pipeline or batch with 100ms windows.
    Want me to go deeper on either?"
    Task: status → awaiting_user
    Voice: SPEAKING → AWAITING_USER_DECISION
16. You: "Streaming pipeline sounds right, tell Forge to prototype it"
17. Ada Executive:
    New task tsk_20260402_003 from Arch's spec + your decision
    Dispatched to Forge (structured packet)
    [noteboard] updated: approach ✓, status: Forge prototyping
    [speaks] "Done. Forge is on it. I'll validate the code when he's done."
    [journal] action: dispatch, agent: FORGE, band: 2, budget: normal
18. You walk away. Voice: ATTENTIVE → IDLE. Executive: WAITING_BACKGROUND.
19. ... 30 minutes later ...
20. Forge submits code.
    Executive validates:
    Gate 1: code artifact ✓
    Gate 2: tests pass ✓, compiles ✓
    Gate 3: implements streaming pipeline per spec ✓
    Gate 4: quality sufficient → delayed delivery (next session)
    Task: done
    [journal] action: validate, gates: [pass, pass, pass, delay], band: 2
21. Next "Hey Ada":
    "Welcome back. Forge finished the streaming prototype.
    Tests pass, 28fps achieved on test data. Want to review?"
    [journal] action: deliver, task: tsk_20260402_003, class: delayed
```

## Dependencies (install order)

```bash
# 1. System packages
sudo apt install libportaudio2 portaudio19-dev build-essential docker.io docker-compose

# 2. Rust toolchain (for claw-code-parity)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# 3. Ollama + voice brain
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gpt-oss:20b    # ~14GB, fits RTX 5060 Ti

# 4. claw-code-parity (agent harness)
git clone https://github.com/ultraworkers/claw-code-parity.git ~/GitHub/claw-code-parity
cd ~/GitHub/claw-code-parity/rust
cargo build --release
# Binary at: target/release/claw

# 5. Configure harness → Ollama
echo 'export OPENAI_API_KEY="ollama"' >> ~/.bashrc
echo 'export OPENAI_BASE_URL="http://localhost:11434/v1"' >> ~/.bashrc
source ~/.bashrc

# 6. Verify harness
./target/release/claw --model gpt-oss:20b

# 7. OpenClaw (Arch/Forge harness — DeepSeek V3.2)
npm install -g openclaw  # or clone openclaw/openclaw repo
echo 'export DEEPSEEK_API_KEY="sk-your-key"' >> ~/.bashrc
source ~/.bashrc
# Configure ~/.openclaw/openclaw.json with DeepSeek provider (see Phase 0)
openclaw --prompt "Hello, verify tool calling works"

# 8. PostgreSQL + pgvector + TimescaleDB (Docker)
cat > ~/GitHub/AdaOS/docker-compose.yml << 'EOF'
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
    command: postgres -c shared_preload_libraries='timescaledb'
volumes:
  ada_pgdata:
EOF
docker-compose up -d
psql -h localhost -U ada -d ada -c "CREATE EXTENSION vector; CREATE EXTENSION timescaledb;"

# 9. Python (in ~/venvs/voice)
python3 -m venv ~/venvs/voice
source ~/venvs/voice/bin/activate
pip install "pipecat-ai[silero,whisper,kokoro]"
pip install faster-whisper
pip install openwakeword
pip install asyncpg          # async PostgreSQL driver
pip install websockets
pip install python-dotenv
pip install sentence-transformers  # for local embedding model (bge-small-en)

# 10. Verify full stack
ollama run gpt-oss:20b "Hello, can you call a tool?" --format json
psql -h localhost -U ada -d ada -c "SELECT 1;"
```

## Files Created Per Phase

| Phase | New Files | Modified |
|-------|-----------|----------|
| 0 | ~/.ada/instructions.md, scripts/test_audio.py, decision/intent_schema.json, docker-compose.yml | pyproject.toml |
| 1 | main.py, config.py, state.py, pipeline/factory.py, pipeline/frames.py | — |
| 2 | memory/schema.sql, memory/store.py, memory/consolidation.py, memory/context_builder.py, memory/compactor.py, memory/models.py, journal/*.py | main.py |
| 3 | noteboard/*.py, ui/noteboard.html, realtime/streamer.py | main.py, pipeline/factory.py |
| 4 | decision/*.py (event, classifier, resolver, router), brain/*.py | pipeline/factory.py |
| 5 | executive/*.py (task_manager, dispatcher, validator, retry_engine), agents/*.py, ideas/*.py, tools/core.py | main.py, config.py |
| 6 | tools/registry.py, tools/extended.py | tools/core.py |
| 7 | voice/*.py, systemd/adaos.service | main.py, pipeline/factory.py |
| 8 | executive/hooks.py, .ada/hooks.yaml | executive/validator.py, executive/task_manager.py |
| 9 | executive/health.py | config.py, state.py |
| 10 | ada/ultraplan/*.py (7 files) | executive/dispatcher.py, config.py |
