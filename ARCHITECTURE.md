# AdaOS Architecture
Voice-first autonomous system. Intelligence separated from execution. All real work done through controlled, iterative workers.

```
Voice → Ada → Task → Worker → Tools → Result → Validation → Ada → Voice
```

---

## Operating Doctrine

Ada is an operational extension of Daniel, not a cautious assistant.

- High-trust local operator — aggressive execution, not permission-seeking
- Rollback-capable environment — git-backed, reversible by default
- Human-guided direction — not human approval for every step
- Asks for direction, irreversibility, and strategic judgment — handles everything else

> Escalate only for direction, irreversibility, and budget boundary. Handle everything else autonomously.

---

## The Layers

### 1. Voice & Personality Layer

A natural, streaming voice interface. First audio reaches Daniel's ears ~1-2s after he stops talking.

- Natural conversation, personality, tone
- Real-time streaming: LLM tokens → sentence chunks → TTS audio → speaker (no batch delay)
- Barge-in / interruption (TTS cancelled immediately when Daniel speaks)
- Consistent voice identity across all turns (same speaker embedding)

**This layer does NOT execute tasks. It only understands and communicates.**

Stack:
- STT: **Granite 4.0 1B Speech** (IBM, #1 OpenASR, ~2GB VRAM, Apache 2.0)
- TTS: **CosyVoice 2 0.5B** (5.5 MOS, 150ms streaming, ~2-4GB VRAM, Apache 2.0)
- TTS fallback: Kokoro 82M (CPU, if CosyVoice unavailable)
- VAD: Silero (16kHz, runs after input resampling)
- Pipeline: Pipecat (streaming architecture, sentence aggregation, audio transport)

Voice identity:
- CosyVoice 2 uses **zero-shot voice cloning** from a reference audio clip
- Reference clip defines Ada's voice — same voice every turn (75.7% speaker similarity)
- Loaded once at startup, cached, never regenerated mid-session

Streaming flow:
```
Mic(44.1kHz) → Resample(16kHz) → VAD → Granite STT (streaming)
             → Mistral Nemo (streaming tokens)
             → Sentence Aggregator (buffers until "." / "!" / "?")
             → Sesame CSM TTS (per-sentence, streaming audio)
             → Resample(44.1kHz) → Speaker
             
Target: first audio ~1-2s after end of speech
```

Hardware:
```
RTX 4070 (12GB) → Granite STT (~2GB) + CosyVoice 2 TTS (~3GB) = ~5GB (headroom!)
CPU              → Kokoro TTS (fallback only) + embeddings
```

---

### 2. Ada (Control Plane)

The brain. Converts conversation into structured intent, creates tasks, routes to workers, validates results.

Ada separates thinking from doing.

Responsibilities:
- Intent classification (structured JSON via Ollama `format` parameter)
- Action resolution (deterministic code, not LLM)
- Task lifecycle management (create, assign, validate, retry, deliver)
- Agent dispatch to workers
- Budget enforcement
- Memory consolidation (episodic → semantic)
- Proactive behaviors (summaries, reminders, drift detection)
- Scheduling and conditional triggers

Two-model strategy:
- **Classification**: Qwen3 14B — strong structured JSON output, 15 closed-set intents
- **Response generation**: Mistral Nemo 12B — warmest conversational tone at this tier, streams well
- APU hot-swaps between them on the same GPU (both fit in 16GB individually)

Hardware:
```
RTX 5060 Ti (16GB) → Mistral Nemo 12B (~8GB Q4) or Qwen3 14B (~9GB Q4) via Ollama
                     APU swaps based on which is needed (classification vs response)
```

#### Decision Engine (the atom)

Every input produces exactly one **Decision Event**.

LLM classifies intent (15 closed-set intents). Code resolves action (16 closed-set actions). Clean separation.

```
Input → Classification (~50-80ms, JSON Schema enforced)
      → Action Resolution (~1ms, deterministic Python)
      → Response Generation (~100-150ms, free-form)
      → Total to first audio: ~250-350ms
```

Intents: brainstorm, note, dispatch, question, decision, command, followup, greeting, farewell, acknowledgement, clarification, noop, agent_delivery, timer_trigger, system_event

Actions: create_note, update_note, promote_note, create_task, update_task, dispatch_agent, respond_only, ask_clarification, escalate_to_user, save_silently, execute_command, validate_result, retry_dispatch, deliver_result, queue_ultraplan, noop

#### State Machine (two concurrent layers)

Voice: IDLE → ATTENTIVE → LISTENING → PROCESSING_FAST → SPEAKING → ATTENTIVE
Executive: IDLE → DISPATCHING → WAITING_BACKGROUND → VALIDATING → IDLE
Overlay: DEGRADED, ERROR, AWAITING_USER_DECISION

#### Task Object

```json
{
  "task_id": "tsk_20260404_001",
  "title": "...",
  "type": "architecture | implementation | research | validation | note_followup | system",
  "status": "draft | queued | dispatched | in_progress | awaiting_result | awaiting_user | validating | done | failed | cancelled",
  "priority": "low | medium | high | critical",
  "owner": "ADA | ARCH | FORGE | DANIEL",
  "brief": "...",
  "success_criteria": [],
  "constraints": [],
  "artifacts_expected": [],
  "artifacts_received": [],
  "retry_count": 0,
  "max_retries": 3,
  "budget_class": "free | cheap | normal | expensive",
  "created_at": "ISO_TIMESTAMP",
  "updated_at": "ISO_TIMESTAMP"
}
```

#### Note → Task Promotion

Notes: `captured → pinned → candidate_task → promoted_to_task → archived`

A note becomes a task when it implies executable commitment:
- Explicit command ("send to Arch", "prototype this")
- Clear actionable structure
- Ada asks, user confirms

#### Policy Bands

- **Band 1 — Free autonomous:** notes, memory, search, local work, speech
- **Band 2 — Autonomous with logging:** repo edits, dispatches, retries, config changes
- **Band 3 — Escalate once:** irreversible actions, budget overrun, architecture pivots

#### Outbox Pattern (restart-safe)

Every side effect (dispatch, notification) writes an outbox event in the SAME PostgreSQL transaction as the state change. Separate worker processes outbox. Survives restarts.

---

### 3. Worker Layer (Execution) — LangGraph

Workers only execute. They do not decide. One framework, one surface.

**LangGraph** is the single execution framework for all worker tasks. It provides:
- Stateful, graph-based agent workflows with cycles and branching
- Built-in persistence (checkpointing) — survives crashes, resumable
- Human-in-the-loop support (Ada can pause/approve/reject mid-workflow)
- Tool calling with any LLM provider (DeepSeek V3.2, GPT-OSS local, etc.)
- Streaming output — Ada sees progress as workers execute

**Why LangGraph, not OpenHands + OpenCode:**
- One framework instead of two — smaller surface, less to maintain
- LangGraph graphs are Python code — Ada's codebase controls the workflow logic
- No subprocess spawning, no workspace management, no CLI wrapping
- Checkpointing gives us restart-safety without the outbox pattern for worker state
- Same graph can run with different LLMs (local GPT-OSS for cheap tasks, DeepSeek for complex)

#### Worker Graphs

```python
# Two pre-built graphs, same framework:

ARCH_GRAPH:  research → analyze → synthesize → review → deliver
FORGE_GRAPH: plan → implement → test → fix → deliver
             (cycles back to implement on test failure, max 3 cycles)
```

Both graphs:
- Receive a structured task packet from Ada
- Call tools (file ops, bash, web search) via LangGraph tool nodes
- Produce artifacts (specs, code, reports)
- Stream progress back to Ada for live noteboard updates
- Checkpoint state at every node — crash-safe

#### Dispatch Model

```
Ada creates Task → writes to PostgreSQL + outbox
Outbox worker → invokes LangGraph (ARCH_GRAPH or FORGE_GRAPH)
Graph runs with DeepSeek V3.2 (or GPT-OSS local for free tasks)
Graph streams progress → Ada updates noteboard
Graph completes → artifacts returned to Ada
Ada validates result through validation layer
```

Agent roles:
- **ARCH** — system design, tradeoffs, research → ARCH_GRAPH + DeepSeek V3.2
- **FORGE** — implementation, code, repo mutation → FORGE_GRAPH + DeepSeek V3.2

Cloud budget: **$100/month** via DeepSeek V3.2 API
- $0.26/$0.38 per MTok → ~333M tokens/month → ~6,600 tasks
- Fallback: OpenRouter (5.5% markup) if DeepSeek down

---

### 4. Knowledge Layer (Onyx)

Provides search and retrieval across documents and systems. Supplies context to Ada BEFORE execution.

The system knows before it acts.

- Document search (repos, files, notes, specs)
- Semantic retrieval (vector search)
- System integration (GitHub, local repos, project context)
- Feeds into Ada's context builder

---

### 5. Tool Layer

Modular capabilities, not intelligence. MCP-ready.

- OCR
- APIs (web fetch, web search)
- Browser automation
- File system operations
- Terminal / bash (sandboxed)
- Search (SearXNG via MCP)

Tools are exposed to workers. Ada doesn't call tools directly — workers do.

---

### 6. Validation Layer
Checks outputs from workers. Enforces correctness. Prevents bad results from reaching Daniel.

4 gates, applied in order:

1. **Structural** — did the worker return the right artifact type? Format correct? (Deterministic, no LLM)
2. **Technical** — does code compile? Tests pass? (Sandboxed execution — bwrap/Docker, no credentials, no network)
3. **Semantic** — does this match the original intent? (LLM-assisted)
4. **Attention** — should this interrupt Daniel now? (Ada judgment: immediate / delayed / silent storage)

#### TRIBE v2 + CRITIC_SERVICE (Gate 4 extension — neural-guided response selection)

**The moat.** This is what separates AdaOS from every other AI assistant.

TRIBE v2 (Meta FAIR) is a trimodal brain encoder that predicts fMRI brain responses to any stimulus — text, audio, video. It produces ~70K voxels of predicted cortical activation. It won the Algonauts 2025 brain encoding competition and generalizes zero-shot to new subjects and languages.

**The insight:** Every response Ada speaks is a stimulus to Daniel's brain. TRIBE v2 can predict the neural response BEFORE Ada speaks. This means we can optimize for actual brain comprehension, not surface-level preference. A reward model learns what users click. TRIBE predicts what the brain DOES. Those are different things.

**Two-tier critic architecture:**

```
Tier 1 — GPT-OSS CRITIC (baseline, always available)
  Ada generates 2-4 candidate responses
  GPT-OSS 20B self-scores candidates (different prompt, same model)
  Scores: relevance, tone, clarity, urgency-match
  → picks best candidate via policy code
  → works without TRIBE, production-ready from day 1

Tier 2 — TRIBE v2 NEURAL CRITIC (experimental enhancement, the moat)
  Feed each candidate (as text or synthesized audio) through TRIBE v2
  TRIBE predicts Daniel's neural response for each candidate
  Extract signals from predicted brain activation:
    - Comprehension regions active? (good)
    - Confusion / cognitive overload patterns? (bad)
    - Attention engagement level? (right amount, not too much)
    - Language processing fluency? (smooth comprehension)
  Score candidates by neural response quality
  → TRIBE ranking combined with GPT-OSS ranking
  → policy code makes final decision
```

**What TRIBE adds over plain self-scoring:**
- Self-scoring asks "which response is better?" — the model judges itself (circular)
- TRIBE asks "which response will Daniel's brain actually comprehend best?" — grounded in neuroscience
- Self-scoring optimizes for what the LLM thinks is good. TRIBE optimizes for what the brain actually does.
- This is the difference between a recommendation algorithm (what you click) and neural engagement (what you understand)

**TRIBE is NOT in the fast turn loop.** During live back-and-forth, Ada runs the normal low-latency path: STT → control model → immediate response. TRIBE stays off.

TRIBE is active when Ada has TIME to think:
- IDLE / ATTENTIVE states — Ada is considering whether to speak proactively
- Background task completion — score whether to interrupt and which candidate to use
- Morning briefing — rank which items to lead with for best neural engagement
- Mid-day check-in — evaluate if now is a good time based on recent audio patterns
- Response polishing — when a 200ms delay is acceptable, run TRIBE for better quality

**TRIBE is OFF during:**
- LISTENING → PROCESSING_FAST → SPEAKING (live turn-taking)
- Barge-in handling
- Any direct user-initiated conversation where latency matters

**CRITIC_SERVICE (unified interface):**
- Inputs: rolling user audio window, transcript window, candidate messages, current system state
- Outputs: interrupt_score, speak_now_score, cognitive_load_score, candidate_rank, confidence, neural_activation_summary (when TRIBE available)
- Tier 1 (GPT-OSS): always runs, ~50ms, baseline scoring
- Tier 2 (TRIBE): runs when time allows, adds neural signal, experimental

**Policy (code, not LLM):**
- Hard block for proactive interruptions below threshold
- Soft guidance for polishing above threshold
- Disabled during active turn-taking
- TRIBE signal weighted into final score when available, not a veto

**Research path:**
1. Build Tier 1 (GPT-OSS self-scoring) — production-ready, validates the candidate generation + ranking pipeline
2. Integrate TRIBE v2 model (facebook/tribev2 from HuggingFace) — predict neural response per candidate
3. Define "healthy neural activation" profile — high comprehension, low confusion, appropriate engagement
4. A/B test: does TRIBE-guided selection produce responses Daniel actually prefers?
5. Iterate: tune the neural activation weighting, find which brain regions matter most for conversational quality
6. Long-term: TRIBE learns Daniel's specific neural patterns from feedback, becomes personalized

Ada still owns intent classification, action resolution, tasking, and final policy. The CRITIC_SERVICE (Tier 1 + Tier 2) only influences WHICH candidate gets spoken and WHETHER Ada should speak at all.

Retry policy (hard cap):
```
Retry 1 — automatic, specific error feedback
Retry 2 — reformulated brief with more context
Retry 3 — escalated with full diagnostics
After 3  — STOP, report to Daniel
```

Gate 2 sandbox (required before autonomous repo write):
- Read-only repo mount
- No credentials in environment
- No network access
- Resource limits (CPU, memory, timeout)

---

### 7. Memory Layer

PostgreSQL 16 + pgvector + TimescaleDB. One database, one transaction, all memory types.

```
PostgreSQL 16 (Docker container)
  ├── pgvector        → semantic search (embeddings, cosine similarity)
  ├── TimescaleDB     → time-partitioned episodes (what happened when)
  ├── Standard tables → tasks, notes, journal, sessions
  ├── Recursive CTEs  → lightweight entity-relationship graph
  └── Outbox table    → restart-safe side effect delivery
```

Brain-inspired memory:

| Brain System | Memory Type | Implementation |
|---|---|---|
| Hippocampus | Episodic | TimescaleDB hypertable (timestamped, append-only) |
| Neocortex | Semantic | pgvector embeddings + standard tables (extracted facts) |
| Association cortex | Relational | Recursive CTEs (entity-relationship edges) |
| Basal ganglia | Procedural | Standard tables (tool recipes, workflows) |
| Sleep replay | Consolidation | Background LLM extracts facts from episodes |

Temporal knowledge: memories have `valid_from` / `valid_until` — handles contradictions over time.

Consolidation runs periodically (every 30 min or after conversation):
1. Read unconsolidated episodes
2. LLM extracts facts, preferences, decisions
3. Compare against existing memories (vector similarity)
4. Write new or update existing (ADD / UPDATE / NOOP)
5. Update entity-relationship graph

Embedding model: BAAI/bge-small-en-v1.5 (384-dim, CPU, local)

---

## Noteboard (Live Canvas)

Visual surface that distills conversation in real-time.

```
┌─────────────────────────────────────┐
│ LIVE                                │
│ • Idea: sensor fusion for SportWave │
│   → Scheduled: discuss with Arch    │
│ TASKS                               │
│ • tsk_001: analytics → ARCH         │
│ SCHEDULED                           │
│ • Call supplier (Friday)            │
└─────────────────────────────────────┘
```

Bidirectional WebSocket. Notes persist in PostgreSQL. Personal notes never visible to workers.

---

## Configurable Model Profiles

```python
# config.py — Voice V2 stack

# Classification (structured JSON output)
CLASSIFY_MODEL = "qwen3:14b"           # Strong JSON schema adherence, 15 intents

# Response generation (conversational warmth)
RESPONSE_MODEL = "mistral-nemo:12b"    # Warmest tone at this tier, streams well

# STT
STT_ENGINE = "granite-speech"           # IBM Granite 4.0 1B Speech (#1 OpenASR)

# TTS
TTS_ENGINE = "sesame-csm"              # Sesame CSM 1B (4.7 MOS, streaming, conversational)
TTS_VOICE = "ada_v1"                   # Fixed speaker embedding — same voice every turn
TTS_FALLBACK = "kokoro"                # CPU fallback if Sesame unavailable

# Embeddings + planning
EMBED_MODEL = "bge-small-en"           # 384-dim, CPU, local
PLAN_MODEL = "qwen3:72b"              # UltraPlan overnight (both GPUs)

# Workers
WORKER_MODEL = "deepseek-chat"         # ARCH/FORGE via DeepSeek V3.2 API
WORKER_API_BASE = "https://api.deepseek.com/v1"

# Budget
MONTHLY_BUDGET = 100.00
```

---

## APU — Adaptive Processing Unit

Ported from AlchemyOS. Manages GPU/VRAM allocation with priority-based eviction.
VRAM flows like water — models swap on and off GPU based on what Daniel is doing.

```
Daniel talking   → Voice model on GPU_0 (fast, conversation)
Daniel idle 3min → Unload voice, load coding model (free local work)
Daniel returns   → Unload coding, reload voice (3-5 sec swap)
Overnight        → Unload everything, load Qwen 72B (planning)
Morning          → Restore voice model, ready for greeting
```

Priority tiers (higher = harder to evict):
- **P0 RESIDENT** — Whisper STT (never evicted, always on GPU_1)
- **P1 VOICE** — Ada's brain (evicted only when Daniel goes idle)
- **P2 CODING** — Gemma 4 26B or similar (yields to voice immediately)
- **P3 PLANNING** — Qwen 72B (overnight only, takes both GPUs)

Features:
- Priority-based eviction with rollback on failed loads
- Thrashing detection (model evicted/reloaded 3+ times in 120s → alert)
- Per-GPU async locks (no concurrent loads to same GPU)
- Ref counting (models can't be evicted during active inference)
- Reconciliation with Ollama on startup (sync registry with reality)
- Hardware monitor via pynvml (GPU temp, VRAM, utilization)
- Inference gateway (all LLM calls go through APU — ensures model is loaded first)

## Hardware Allocation

```
RTX 5060 Ti (16GB) → Mistral Nemo 12B (~8GB) OR Qwen3 14B (~9GB) — swapped by APU
RTX 4070 (12GB)    → Granite STT (~2GB, RESIDENT) + CosyVoice 2 TTS (~3GB, RESIDENT)
CPU (128GB RAM)    → Kokoro fallback + embeddings + consolidation
Cloud              → DeepSeek V3.2 (workers, $100/month)
```

Overnight (UltraPlan): APU unloads GPU_0 models, loads Qwen 72B across both GPUs + RAM overflow.
GPU_1 (RTX 4070) keeps Granite+Sesame resident — always ready for morning greeting.

---

## Software Stack

```
Ollama              → serves Mistral Nemo 12B + Qwen3 14B, OpenAI-compatible API
Pipecat             → voice pipeline (VAD, streaming STT/TTS, sentence aggregation)
Granite 4.0 1B      → STT (HuggingFace Transformers, local GPU)
CosyVoice 2 0.5B    → TTS (5.5 MOS, 150ms streaming, zero-shot voice cloning)
Kokoro 82M          → fallback TTS (CPU, if Sesame unavailable)
TRIBE v2            → background critic (Gate 4 extension, cognitive load scoring)
LangGraph           → worker execution framework (ARCH_GRAPH + FORGE_GRAPH)
Onyx                → knowledge retrieval layer
PostgreSQL 16       → memory (pgvector + TimescaleDB, Docker)
DeepSeek V3.2 API   → cloud LLM for workers ($0.30/MTok)
```

---

## Execution Phases

### Phase 0: Foundation [DONE]
- [x] Ollama + models (qwen3:14b, qwen3:30b, qwen3:72b)
- [x] LangGraph + langchain-openai
- [x] PostgreSQL 16 (Docker, pgvector + TimescaleDB, 15 tables)
- [x] DeepSeek API key
- [x] Ada's system prompt (Claude Code patterns)
- [x] Intent classification JSON Schema (15 intents)
- [x] Action Resolver (16 actions, deterministic)
- [x] Audio hardware tested (USB Audio DAC, headset)
- [x] Whisper STT tested on RTX 4070
- [x] Kokoro TTS tested
- [x] GPU isolation verified (RTX 4070 + RTX 5060 Ti)

### Phase 1: Voice Loop V1 [DONE]
- [x] Pipecat pipeline: Mic → Resample → VAD → STT → Ada → TTS → Speaker
- [x] Voice state machine: IDLE → ATTENTIVE → LISTENING → SPEAKING
- [x] Auto device detection (headset pairing)
- [x] Sample rate handling (44.1kHz hardware → 16kHz pipeline)
- [x] Barge-in support (Pipecat allow_interruptions)
- [x] Wake word gate (always-listen mode, OpenWakeWord stub)
- [x] Kokoro TTS working

### Phase 1.5: Voice Loop V2 [NEXT]
- [ ] **Fix amnesia**: multi-turn message format (not history-in-system-prompt)
- [ ] **Personality prompt**: few-shot examples, anti-patterns, warm tone
- [ ] **Streaming response**: LLM tokens → sentence aggregator → per-sentence TTS
- [ ] **Mistral Nemo 12B**: pull, test, wire as response model via Ollama
- [ ] **Sesame CSM**: install, test, fixed speaker embedding for Ada's voice
- [ ] **Granite STT**: install, test, replace Whisper
- [ ] **Kokoro fallback**: keep as CPU fallback if Sesame unavailable
- [ ] Target: first audio ~1-2s after end of speech (down from 10-15s)

### Phase 2: Ada Control Plane
- [ ] Decision Engine: classification + action resolution
- [ ] Task lifecycle (create, assign, validate, close)
- [ ] Note → Task promotion
- [ ] Policy band enforcement
- [ ] Journal (append-only action log)
- [ ] Budget tracking

### Phase 3: Memory
- [ ] PostgreSQL schema (episodes, memories, entities, relations, tasks, notes, journal, outbox)
- [ ] Episodic writes (every turn → episodes with embedding)
- [ ] Semantic retrieval (pgvector similarity)
- [ ] Context builder (assemble prompt from all memory types)
- [ ] Consolidation process (background, episodic → semantic)
- [ ] Temporal knowledge (valid_from / valid_until)

### Phase 4: Workers (LangGraph)
- [ ] ARCH_GRAPH: research → analyze → synthesize → review → deliver
- [ ] FORGE_GRAPH: plan → implement → test → fix → deliver (with retry cycle)
- [ ] Tool nodes: file ops, bash (sandboxed), web search
- [ ] LLM integration: DeepSeek V3.2 via langchain-openai (OpenAI-compatible)
- [ ] Checkpointing: PostgreSQL-backed persistence (crash-safe)
- [ ] Streaming: progress updates to Ada/noteboard during execution
- [ ] Outbox pattern (restart-safe dispatch from Ada → LangGraph)
- [ ] Arch/Forge dispatch routing via Action Resolver

### Phase 5: Knowledge (Onyx)
- [ ] Onyx setup and indexing
- [ ] Ada queries Onyx before dispatch
- [ ] Document/repo/project search
- [ ] Context injection into worker prompts

### Phase 6: Validation + CRITIC_SERVICE
- [ ] Gate 1: structural (deterministic)
- [ ] Gate 2: technical (sandboxed — bwrap/Docker)
- [ ] Gate 3: semantic (LLM-assisted)
- [ ] Gate 4: attention-worthiness (base logic)
- [ ] CRITIC_SERVICE Tier 1 (GPT-OSS self-scoring):
  - [ ] Candidate generation: Ada produces 2-4 response variants
  - [ ] GPT-OSS scores candidates (relevance, tone, clarity, urgency-match)
  - [ ] Policy code picks best candidate
  - [ ] Proactive speech requires critic pass, live turns bypass
- [ ] CRITIC_SERVICE Tier 2 (TRIBE v2 neural critic — experimental):
  - [ ] Integrate facebook/tribev2 from HuggingFace
  - [ ] Feed candidates (text/audio) through TRIBE → predicted fMRI activation
  - [ ] Extract signals: comprehension, confusion, engagement, fluency
  - [ ] Define "healthy neural activation" profile
  - [ ] Combine TRIBE ranking with GPT-OSS ranking
  - [ ] A/B test: does TRIBE-guided selection improve response quality?
- [ ] Retry engine (3-tier, hard cap)

### Phase 7: Noteboard
- [ ] WebSocket server (bidirectional)
- [ ] Live note streaming during conversation
- [ ] Note states + task status display
- [ ] UI (localhost, vanilla JS)

### Phase 8: 24/7 Daemon
- [ ] Wake word (OpenWakeWord or button)
- [ ] Systemd service, auto-restart
- [ ] Health checks (Ollama, PostgreSQL, Whisper, TTS, DeepSeek)
- [ ] DEGRADED mode (graceful fallback)
- [ ] Startup resume (pending tasks from outbox)

### Phase 9: UltraPlan (Overnight)
- [ ] Queue complex tasks during day
- [ ] Dual GPUs → Qwen 72B overnight
- [ ] 3-pass: decompose → critique → synthesize
- [ ] Morning voice briefing with results
- [ ] Approve → promote to worker task

---

## Decision Gates (when Ada MUST escalate)

1. Major architecture choices
2. Irreversible actions
3. Budget threshold crossings
4. Ambiguous strategic pivots
5. Conflicting recommendations from workers

Everything else: Ada handles, logs to journal, reports when relevant.

---

## One-line summary

A voice-first system where intelligence is separated from execution, and all real work is done through controlled, iterative workers.
