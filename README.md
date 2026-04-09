# AdaOS

Voice-first autonomous AI operating system. Ada converts free-form human intent into structured, validated execution — locally, with aggressive autonomy and full auditability.

## Architecture

```
Voice (Pipecat) → Decision Engine (Qwen 14B) → Task Manager → Workers (DeepSeek V3.2) → Validation → Delivery
                                                     ↕
                                              Memory (PostgreSQL)
```

**Three-tier team:**

| Agent | Role | Location | Model |
|-------|------|----------|-------|
| **Ada** | Control plane — classify, route, validate, respond | Local (Ollama) | Qwen3 14B |
| **Arch** | System design, research, tradeoff analysis | Cloud (DeepSeek) | DeepSeek V3.2 |
| **Forge** | Implementation, code writing, repo mutation | Cloud (DeepSeek) | DeepSeek V3.2 |

Ada runs locally with high autonomy. Workers run in the cloud with a $100/month budget cap.

## Prerequisites

- **Python 3.11+**
- **Docker** (for PostgreSQL)
- **Ollama** with models pulled:
  - `qwen3:14b` (Ada control plane)
  - `qwen3:72b` (overnight planning, optional)
- **NVIDIA GPUs** (recommended):
  - GPU 0: RTX 5060 Ti 16GB (voice model)
  - GPU 1: RTX 4070 12GB (Whisper STT, resident)
- **Audio input device** (microphone for voice mode)

## Setup

### 1. Clone and install

```bash
git clone git@github.com:NeoSynaptics/AdaOS.git
cd AdaOS
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — add your DeepSeek API key
```

### 3. Start PostgreSQL

```bash
docker compose up -d
```

This starts TimescaleDB (PostgreSQL 16 + pgvector + TimescaleDB) and auto-runs the schema from `ada/memory/schema.sql`.

### 4. Pull Ollama models

```bash
ollama pull qwen3:14b
# Optional: overnight planning
ollama pull qwen3:72b
```

### 5. Run Ada

```bash
# Voice mode (default) — mic → STT → Ada → TTS → speaker
./start.sh

# Text mode — keyboard input
./start.sh --text

# Or directly:
python -m ada.main          # voice
python -m ada.main --text   # text
```

The startup script runs preflight checks (PostgreSQL, Ollama, model availability, audio device) before launching.

## Project Structure

```
ada/
  main.py                  # Entry point — wires all subsystems
  config.py                # Centralized configuration
  state.py                 # Voice + Executive state machines
  system_prompt.py         # Ada's system prompts

  decision/                # Intent classification + action resolution
    classifier.py          # Ollama JSON-schema constrained classification
    resolver.py            # Deterministic action resolver (no LLM)
    router.py              # Agent routing
    intent_schema.json     # 15-intent closed set

  voice/                   # Pipecat voice pipeline
    pipeline.py            # Mic → VAD → Whisper → Ada → TTS → Speaker
    wakeword.py            # OpenWakeWord detection

  apu/                     # GPU/VRAM orchestration
    orchestrator.py        # Priority-based model eviction + loading
    gateway.py             # Auto-load models before LLM calls
    registry.py            # Model fleet registry
    monitor.py             # Hardware monitoring
    models.py              # ModelCard, tiers, GPU enums

  memory/                  # PostgreSQL + pgvector + TimescaleDB
    store.py               # Async connection pool + queries
    models.py              # ORM models
    schema.sql             # Full database schema
    embeddings.py          # BAAI/bge-small-en-v1.5
    context_builder.py     # Assembles system prompt from memory
    consolidation.py       # Episodic → semantic memory extraction

  executive/               # Task execution + validation
    task_manager.py        # Task lifecycle
    validator.py           # 4-gate validation
    critic.py              # TRIBE v2 response scoring
    outbox.py              # Restart-safe side effect delivery

  agents/                  # Worker dispatch
    dispatcher.py          # LangGraph graphs (Arch + Forge)

  ultraplan/               # Overnight deep planning
    daemon.py              # Background daemon (23:00-07:00)
    planner.py             # 3-pass: decompose → critique → synthesize
    queue.py               # PostgreSQL-backed task queue
    templates.py           # Planning prompt templates
    config.py              # UltraPlan configuration

  noteboard/               # Live UI
    server.py              # WebSocket server
    renderer.py            # HTML/CSS rendering
    noteboard.html         # Frontend

  journal/                 # Append-only execution log
    logger.py              # Journal entry recording

  tools/                   # Tool interface
    core.py                # Core tool implementations
    registry.py            # Tier-based tool loading
```

## Core Concepts

### Decision Event (the atom)

Every input produces exactly one Decision Event:

```
Input → Classification (50-80ms, JSON Schema) → Action Resolution (1ms, deterministic) → Response (100-150ms)
```

Total: ~250-350ms to first audio. The Decision Event captures source, classification, decision, execution, and side effects — enabling full replay and debugging.

### Policy Bands

| Band | Behavior | Examples |
|------|----------|---------|
| **1** | Free autonomous | Notes, memory search, internal queries |
| **2** | Autonomous + logged | Repo edits, agent dispatches, task creation |
| **3** | Escalate to user | Irreversible actions, architecture pivots, budget > threshold |

### APU (Adaptive Processing Unit)

Models swap on/off GPU automatically based on activity:

- **Daniel talking** → voice model on GPU_0
- **Daniel idle 3min** → swap to coding model
- **Overnight (23:00-07:00)** → both GPUs for Qwen 72B planning

See [APU.md](APU.md) for details.

### Memory System

Brain-inspired memory with episodic → semantic consolidation:

- **Episodes** (hippocampus) — raw conversation turns, TimescaleDB hypertable
- **Memories** (neocortex) — extracted facts, pgvector semantic search
- **Entities + Relations** — knowledge graph with recursive CTEs
- **Consolidation** (sleep replay) — LLM extracts facts from episodes every 30 min

See [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) for details.

### UltraPlan (Overnight Planning)

Deep planning with a 72B model while you sleep:

```
submitted → claimed → planning (3 passes) → completed → reviewed (morning briefing)
```

See [ULTRAPLAN.md](ULTRAPLAN.md) for details.

## Configuration

All configuration lives in `ada/config.py` as dataclasses with sensible defaults:

| Config | Purpose |
|--------|---------|
| `AdaConfig` | Top-level config aggregator |
| `APUConfig` | GPU/VRAM thresholds and timing |
| `ModelConfig` | Model names and API endpoints |
| `TTSConfig` | TTS engine selection |
| `TRIBEConfig` | Neural critic settings |
| `BudgetConfig` | Cloud spending limits |
| `DatabaseConfig` | PostgreSQL connection |
| `VoiceConfig` | STT model and wake word |

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full system architecture, layer by layer |
| [DECISION_ENGINE.md](DECISION_ENGINE.md) | Decision Event atom + 20 traced conversations |
| [TASK_SCHEMA.md](TASK_SCHEMA.md) | Task object, lifecycle, validation gates |
| [TOOL_POLICY.md](TOOL_POLICY.md) | Policy bands, tool tiers, escalation rules |
| [RUNTIME_STATE_MACHINE.md](RUNTIME_STATE_MACHINE.md) | Voice + Executive FSMs |
| [EXECUTION.md](EXECUTION.md) | Implementation phases, preflight, gap fixes |
| [PLAN.md](PLAN.md) | Implementation roadmap |
| [APU.md](APU.md) | GPU/VRAM orchestration subsystem |
| [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) | Memory architecture and consolidation |
| [ULTRAPLAN.md](ULTRAPLAN.md) | Overnight deep planning subsystem |
| [NOTEBOARD.md](NOTEBOARD.md) | Live WebSocket UI |

## Development Status

| Subsystem | Status |
|-----------|--------|
| Decision Engine | ~90% — classifier + resolver working |
| Voice Pipeline | ~80% — Pipecat + Whisper integrated |
| Memory | ~85% — schema, consolidation, context builder done |
| APU | ~85% — eviction, registry, thrashing detection done |
| Executive | ~75% — task manager + validator, some stubs |
| Agent Dispatch | ~40% — LangGraph structure, graphs not filled in |
| UltraPlan | ~30% — queue + daemon exist, planner has TODOs |
| Noteboard | ~20% — WebSocket server, frontend not built |
| Tests | ~5% — no test suite yet |

## License

Private. NeoSynaptics.
