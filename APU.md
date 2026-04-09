# APU (Adaptive Processing Unit)

The APU manages GPU/VRAM as a shared resource across Ada's model fleet. Models load and unload automatically based on activity, priority, and time of day — like an operating system's memory manager, but for VRAM.

Ported from AlchemyOS StackOrchestrator, adapted for AdaOS.

## Hardware Layout

| GPU | Card | VRAM | Role |
|-----|------|------|------|
| GPU_0 | RTX 5060 Ti | 16 GB | Voice model (daytime) / Planning (overnight) |
| GPU_1 | RTX 4070 | 12 GB | Whisper STT (resident) / Planning (overnight) |
| CPU | — | 128 GB RAM | Embeddings, TTS fallback, model staging |

Safety margin: 512 MB kept free on each GPU for overhead.

## Model Tiers

Priority tiers determine eviction order (higher priority = harder to evict):

| Tier | Priority | Model | VRAM | Behavior |
|------|----------|-------|------|----------|
| **RESIDENT** | P0 | faster-whisper | 2 GB | Never evicted. Always on GPU_1 |
| **VOICE** | P1 | Qwen3 14B | 9 GB | Ada's brain. Evicted only on extended idle |
| **CODING** | P2 | Gemma 4 26B | 15 GB | Background coding. Yields to voice immediately |
| **PLANNING** | P3 | Qwen3 72B | 45 GB | Overnight only. Takes both GPUs + RAM overflow |

## Lifecycle

### Daytime (07:00 - 23:00)

```
Daniel talking → Voice model on GPU_0 (conversation mode)
                 Whisper on GPU_1 (always)

Daniel idle 3 min → Unload voice model
                    Load coding model on GPU_0

Daniel returns → Unload coding model
                 Load voice model (3-5s swap)
```

### Overnight (23:00 - 07:00)

```
Overnight window starts → Unload voice + coding models
                          Load Qwen 72B across both GPUs
                          UltraPlan daemon runs planning tasks

Morning (07:00) → Unload planning model
                  Load voice model
                  Ada ready for morning greeting
```

## Core Operations

### ensure_loaded(model_name, gpu)

The main entry point. Ensures a model is on GPU and ready for inference.

1. Already on GPU? → return immediately
2. Room on target GPU? → load directly
3. No room? → evict lowest-priority models until room exists
4. All higher priority? → fail with error
5. Load the model via Ollama API
6. Load fails? → rollback (re-load evicted models)

All operations hold an async lock per GPU to prevent concurrent loads from corrupting state.

### Eviction Strategy

When GPU space is needed:

1. Get all models on the target GPU, sorted by tier (lowest first), then by last-used time
2. Skip models that are:
   - RESIDENT tier (never evicted)
   - Currently in use (`ref_count > 0`)
   - Higher priority than the requesting model
3. Evict candidates one at a time until enough VRAM is freed
4. If any eviction fails → rollback all previous evictions

### Thrashing Detection

If a model is evicted and reloaded 3+ times within 120 seconds, the APU logs a thrashing warning. This indicates a configuration problem (e.g., two models competing for the same GPU).

### Rollback

If a model load fails after evictions were performed, the APU re-loads the evicted models to restore the previous state. Best-effort: if rollback also fails, the error is logged and the system continues.

## Components

### APUOrchestrator (`ada/apu/orchestrator.py`)

Central controller. Public API:

| Method | Purpose |
|--------|---------|
| `ensure_loaded(model, gpu)` | Load model with auto-eviction |
| `unload(model)` | Free VRAM |
| `transition_to_voice_mode()` | Ensure voice model loaded |
| `transition_to_coding_mode()` | Swap voice → coding |
| `transition_to_planning_mode()` | Unload all, load 72B |
| `release_planning_mode()` | Unload 72B, restore voice |
| `reconcile()` | Sync registry with Ollama state |
| `status()` | Full APU status for debugging |

### APUGateway (`ada/apu/gateway.py`)

Transparent proxy between Ada and Ollama. Before any LLM call, the gateway:

1. Calls `orchestrator.ensure_loaded(model)` to guarantee the model is on GPU
2. Increments `ref_count` on the model card (prevents eviction during inference)
3. Forwards the request to Ollama
4. Decrements `ref_count` when done

Ada never calls Ollama directly — all calls go through the gateway.

### ModelRegistry (`ada/apu/registry.py`)

In-memory registry of all managed models. Tracks:

- Model metadata (name, tier, VRAM requirements)
- Current location (GPU_0, GPU_1, CPU_RAM, disk)
- Ref counts and last-used timestamps
- VRAM budget per GPU

Provides:
- `eviction_candidates(gpu, needed_mb)` — sorted candidates for eviction
- `gpu_free_mb(gpu)` — available VRAM on a GPU
- `status_summary()` — full fleet status

### HardwareMonitor (`ada/apu/monitor.py`)

Polls GPU state (VRAM usage, temperature, utilization) via nvidia-smi. Provides `HardwareSnapshot` objects.

### Models (`ada/apu/models.py`)

Data classes:

- `ModelCard` — metadata for a managed model
- `ModelTier` — priority enum (RESIDENT through PLANNING)
- `ModelLocation` — where a model lives (GPU_0, GPU_1, GPU_SPLIT, CPU_RAM, DISK)
- `GPU` — physical GPU enum
- `GPUSnapshot` / `HardwareSnapshot` — hardware state
- `LoadResult` — result of load/unload operations
- `APUEvent` — audit trail event

## Audit Trail

Every operation (load, unload, evict, rollback, thrash, error) is recorded as an `APUEvent` in a ring buffer (last 1000 events). Available via `orchestrator.status()` for debugging.

## Idle Monitor

A background task in `ada/main.py` checks voice state every 10 seconds:

- Voice active → ensure voice model on GPU
- Voice idle > 180s → swap to coding model
- Overnight window + idle → transition to planning mode

## Ollama Integration

The APU uses Ollama's HTTP API:

- **Load model:** `POST /api/generate` with `keep_alive: "10m"` and empty prompt
- **Unload model:** `POST /api/generate` with `keep_alive: 0`
- **Check running:** `GET /api/ps`

## Implementation Status

- Orchestrator (eviction, loading, rollback): complete
- Gateway (auto-load, ref counting): complete
- Registry (fleet management): complete
- Monitor (hardware polling): complete
- Thrashing detection: complete
- Idle monitor: complete
- Reconciliation with Ollama: complete
