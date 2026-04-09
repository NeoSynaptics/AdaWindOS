"""APU Orchestrator — GPU/VRAM management with priority-based eviction.

Ported from AlchemyOS StackOrchestrator, adapted for AdaOS.

Core flow:
  1. Ada needs a model → orchestrator.ensure_loaded(model)
  2. Orchestrator checks: is it already on GPU?
  3. If not: is there room? If yes: load.
  4. If no room: evict lowest-priority models until room exists.
  5. If eviction fails (all higher priority or in-use): error.
  6. Load the model. If load fails: rollback (re-load evicted models).
  7. Return success. Model is on GPU, ready for inference.

Error handling at every step. Rollback on failure. Thrashing detection.
Async locks prevent concurrent loads to same GPU from corrupting state.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from .models import (
    ModelCard, ModelTier, ModelLocation, GPU, GPU_VRAM_MB,
    LoadResult, APUEvent, VRAM_SAFETY_MARGIN_MB,
)
from .registry import ModelRegistry
from .monitor import HardwareMonitor

log = logging.getLogger("ada.apu.orchestrator")

# Thrashing: if a model is evicted and reloaded N times in M seconds, alert
THRASH_WINDOW_SEC = 120
THRASH_THRESHOLD = 3


class ThrashDetector:
    """Detects when models are being evicted and reloaded repeatedly."""

    def __init__(self):
        self._events: list[tuple[str, float]] = []  # (model_name, timestamp)

    def record_eviction(self, model: str) -> None:
        self._events.append((model, time.time()))
        self._cleanup()

    def is_thrashing(self, model: str) -> bool:
        self._cleanup()
        count = sum(1 for m, _ in self._events if m == model)
        return count >= THRASH_THRESHOLD

    def _cleanup(self) -> None:
        cutoff = time.time() - THRASH_WINDOW_SEC
        self._events = [(m, t) for m, t in self._events if t > cutoff]


class APUOrchestrator:
    """Manages model placement across GPUs with priority-based eviction."""

    def __init__(
        self,
        registry: ModelRegistry,
        monitor: HardwareMonitor,
        ollama_base_url: str = "http://localhost:11434",
    ):
        self.registry = registry
        self.monitor = monitor
        self.ollama_url = ollama_base_url
        self._thrash = ThrashDetector()
        self._gpu_locks: dict[GPU, asyncio.Lock] = {
            GPU.GPU_0: asyncio.Lock(),
            GPU.GPU_1: asyncio.Lock(),
        }
        self._events: list[APUEvent] = []  # audit trail (ring buffer, last 1000)
        self._max_events = 1000

    # --- Public API ---

    async def ensure_loaded(self, model_name: str, gpu: GPU | None = None) -> LoadResult:
        """Ensure a model is loaded on GPU and ready for inference.

        If already loaded: returns immediately.
        If not loaded: evicts lower-priority models if needed, then loads.
        If load fails: rolls back evictions.

        Args:
            model_name: Ollama model name
            gpu: Preferred GPU (None = use model's preferred_gpu or first available)
        """
        card = self.registry.get(model_name)
        if not card:
            return LoadResult(
                success=False, model=model_name, location=ModelLocation.NOT_AVAILABLE,
                error=f"Model {model_name} not registered with APU",
            )

        # Already on GPU?
        if card.is_on_gpu:
            card.touch()
            log.debug(f"{model_name} already on GPU ({card.location.value})")
            return LoadResult(success=True, model=model_name, location=card.location)

        # Determine target GPU
        target_gpu = gpu or card.preferred_gpu or GPU.GPU_0

        # Check thrashing
        if self._thrash.is_thrashing(model_name):
            log.warning(f"THRASHING detected for {model_name} — loading anyway but this needs investigation")
            self._record_event(APUEvent(
                event_type="thrash", model=model_name,
                from_location=card.location.value, to_location=target_gpu.name,
                reason=f"Evicted/reloaded {THRASH_THRESHOLD}+ times in {THRASH_WINDOW_SEC}s",
            ))

        # Load with eviction if needed
        async with self._gpu_locks[target_gpu]:
            return await self._load_with_eviction(card, target_gpu)

    async def unload(self, model_name: str) -> LoadResult:
        """Unload a model from GPU to save VRAM."""
        card = self.registry.get(model_name)
        if not card:
            return LoadResult(
                success=False, model=model_name, location=ModelLocation.NOT_AVAILABLE,
                error=f"Model {model_name} not registered",
            )

        if not card.is_on_gpu:
            return LoadResult(success=True, model=model_name, location=card.location)

        if card.is_in_use:
            return LoadResult(
                success=False, model=model_name, location=card.location,
                error=f"Model {model_name} is in use (ref_count={card.ref_count}), cannot unload",
            )

        if card.tier == ModelTier.RESIDENT:
            return LoadResult(
                success=False, model=model_name, location=card.location,
                error=f"Model {model_name} is RESIDENT tier, cannot unload",
            )

        return await self._unload_from_gpu(card)

    async def transition_to_voice_mode(self) -> LoadResult:
        """Ensure voice model is loaded, evict coding model if needed.

        Called when Daniel starts talking.
        """
        log.info("Transitioning to VOICE mode")
        voice_models = self.registry.models_by_tier(ModelTier.VOICE)
        if not voice_models:
            return LoadResult(success=False, model="", location=ModelLocation.DISK,
                              error="No VOICE tier model registered")
        return await self.ensure_loaded(voice_models[0].name)

    async def transition_to_coding_mode(self) -> LoadResult:
        """Load coding model, evict voice model if needed.

        Called when Daniel has been silent for attentive_timeout.
        """
        log.info("Transitioning to CODING mode")

        # First unload voice model to free VRAM
        voice_models = self.registry.models_by_tier(ModelTier.VOICE)
        for vm in voice_models:
            if vm.is_on_gpu and not vm.is_in_use:
                await self._unload_from_gpu(vm)

        coding_models = self.registry.models_by_tier(ModelTier.CODING)
        if not coding_models:
            return LoadResult(success=False, model="", location=ModelLocation.DISK,
                              error="No CODING tier model registered")
        return await self.ensure_loaded(coding_models[0].name)

    async def transition_to_planning_mode(self) -> LoadResult:
        """Unload everything, load planning model across both GPUs.

        Called at overnight window start.
        """
        log.info("Transitioning to PLANNING mode — unloading all non-RESIDENT models")

        # Unload everything except RESIDENT
        for card in self.registry.all_models():
            if card.is_on_gpu and card.tier != ModelTier.RESIDENT:
                await self._unload_from_gpu(card)

        planning_models = self.registry.models_by_tier(ModelTier.PLANNING)
        if not planning_models:
            return LoadResult(success=False, model="", location=ModelLocation.DISK,
                              error="No PLANNING tier model registered")

        # Load planning model (might need both GPUs)
        return await self.ensure_loaded(planning_models[0].name)

    async def release_planning_mode(self) -> None:
        """Unload planning model and restore voice mode.

        Called at overnight window end.
        """
        log.info("Releasing PLANNING mode — restoring voice")
        planning_models = self.registry.models_by_tier(ModelTier.PLANNING)
        for pm in planning_models:
            if pm.is_on_gpu:
                await self._unload_from_gpu(pm)

        await self.transition_to_voice_mode()

    async def reconcile(self) -> None:
        """Reconcile registry state with actual Ollama state.

        Call on startup or when state might be out of sync.
        Uses actual GPU ID from Ollama /api/ps response when available,
        falls back to the model's preferred_gpu from registry.
        """
        log.info("Reconciling APU registry with Ollama...")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/ps")
                resp.raise_for_status()
                data = resp.json()

            running = {m["name"]: m for m in data.get("models", [])}

            for card in self.registry.all_models():
                if card.name in running:
                    model_info = running[card.name]

                    # Detect actual GPU from Ollama response
                    # Ollama /api/ps may include gpu_id, gpu_layers, or size_vram fields
                    gpu_id = model_info.get("gpu_id")
                    gpu_layers = model_info.get("details", {}).get("gpu_layers", None)

                    if gpu_id is not None:
                        # Ollama told us which GPU
                        location = ModelLocation.GPU_0 if gpu_id == 0 else ModelLocation.GPU_1
                    elif card.preferred_gpu is not None:
                        # Use registry's preferred GPU as best guess
                        location = (ModelLocation.GPU_0 if card.preferred_gpu == GPU.GPU_0
                                    else ModelLocation.GPU_1)
                    else:
                        # Last resort: check VRAM usage on each GPU to infer
                        snapshot = await self.monitor.snapshot()
                        gpu0_used = snapshot.gpu_free_mb(GPU.GPU_0) if snapshot.gpu(GPU.GPU_0) else 99999
                        gpu1_used = snapshot.gpu_free_mb(GPU.GPU_1) if snapshot.gpu(GPU.GPU_1) else 99999
                        # Model is probably on the GPU with less free VRAM
                        location = ModelLocation.GPU_0 if gpu0_used < gpu1_used else ModelLocation.GPU_1

                    if not card.is_on_gpu:
                        self.registry.update_location(card.name, location)
                        log.info(f"Reconcile: {card.name} found running → {location.value}")
                    elif card.location != location:
                        self.registry.update_location(card.name, location)
                        log.info(f"Reconcile: {card.name} GPU corrected → {location.value}")
                else:
                    if card.is_on_gpu:
                        self.registry.update_location(card.name, ModelLocation.DISK)
                        log.info(f"Reconcile: {card.name} not in Ollama → DISK")

        except httpx.ConnectError:
            log.error("Reconcile failed: cannot connect to Ollama. Is it running?")
        except Exception as e:
            log.error(f"Reconcile failed: {e}", exc_info=True)

    def status(self) -> dict:
        """Full APU status for health checks and debugging."""
        return {
            **self.registry.status_summary(),
            "recent_events": [
                {
                    "type": e.event_type,
                    "model": e.model,
                    "from": e.from_location,
                    "to": e.to_location,
                    "reason": e.reason,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in self._events[-20:]
            ],
        }

    # --- Internal operations ---

    async def _load_with_eviction(self, card: ModelCard, gpu: GPU) -> LoadResult:
        """Load a model, evicting lower-priority models if needed.

        On load failure, rolls back evictions (re-loads evicted models).
        """
        needed_mb = card.vram_mb
        free_mb = await self.registry.gpu_free_mb_real(gpu)
        evicted: list[ModelCard] = []

        # Evict if needed
        if needed_mb > free_mb:
            deficit = needed_mb - free_mb
            candidates = self.registry.eviction_candidates(gpu, deficit)

            if not candidates:
                return LoadResult(
                    success=False, model=card.name, location=card.location,
                    error=f"Cannot free {deficit}MB on {gpu.name} — all models are higher priority or in-use",
                )

            freed = 0
            for victim in candidates:
                if freed >= deficit:
                    break
                result = await self._unload_from_gpu(victim)
                if result.success:
                    evicted.append(victim)
                    freed += victim.vram_mb
                    self._thrash.record_eviction(victim.name)
                else:
                    log.error(f"Failed to evict {victim.name}: {result.error}")
                    # Rollback: re-load what we already evicted
                    await self._rollback_evictions(evicted, gpu)
                    return LoadResult(
                        success=False, model=card.name, location=card.location,
                        error=f"Eviction of {victim.name} failed, rolled back",
                    )

        # Load the model
        start = time.time()
        try:
            target_loc = ModelLocation.GPU_0 if gpu == GPU.GPU_0 else ModelLocation.GPU_1

            async with httpx.AsyncClient(timeout=300.0) as client:
                # Ollama loads model on first generate call with keep_alive
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={
                        "model": card.name,
                        "prompt": "",
                        "keep_alive": "10m",
                    },
                )
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Ollama returned {resp.status_code}: {resp.text[:200]}")

            duration = time.time() - start
            card.load_time_sec = duration
            self.registry.update_location(card.name, target_loc)

            self._record_event(APUEvent(
                event_type="load", model=card.name,
                from_location="disk", to_location=target_loc.value,
                duration_sec=duration,
                reason=f"Evicted {len(evicted)} models" if evicted else "Direct load",
            ))

            log.info(f"Loaded {card.name} on {gpu.name} in {duration:.1f}s"
                      f"{f' (evicted: {[e.name for e in evicted]})' if evicted else ''}")

            return LoadResult(
                success=True, model=card.name, location=target_loc,
                duration_sec=duration, evicted=[e.name for e in evicted],
            )

        except httpx.TimeoutException:
            log.error(f"Loading {card.name} timed out (300s) — rolling back evictions")
            await self._rollback_evictions(evicted, gpu)
            return LoadResult(
                success=False, model=card.name, location=card.location,
                error="Load timed out (300s)",
                evicted=[e.name for e in evicted],
            )

        except httpx.ConnectError:
            log.error(f"Cannot connect to Ollama at {self.ollama_url} — is it running?")
            await self._rollback_evictions(evicted, gpu)
            return LoadResult(
                success=False, model=card.name, location=card.location,
                error=f"Cannot connect to Ollama at {self.ollama_url}",
            )

        except Exception as e:
            log.error(f"Loading {card.name} failed: {e}", exc_info=True)
            await self._rollback_evictions(evicted, gpu)
            return LoadResult(
                success=False, model=card.name, location=card.location,
                error=str(e), evicted=[e.name for e in evicted],
            )

    async def _unload_from_gpu(self, card: ModelCard) -> LoadResult:
        """Unload a model from GPU via Ollama."""
        if card.is_in_use:
            return LoadResult(
                success=False, model=card.name, location=card.location,
                error=f"Cannot unload {card.name}: ref_count={card.ref_count}",
            )

        old_loc = card.location
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": card.name, "prompt": "", "keep_alive": 0},
                )
                # Ollama returns 200 even if model wasn't loaded

            self.registry.update_location(card.name, ModelLocation.CPU_RAM)

            self._record_event(APUEvent(
                event_type="unload", model=card.name,
                from_location=old_loc.value, to_location="cpu_ram",
            ))

            log.info(f"Unloaded {card.name} from {old_loc.value}")
            return LoadResult(success=True, model=card.name, location=ModelLocation.CPU_RAM)

        except httpx.ConnectError:
            log.error(f"Cannot connect to Ollama to unload {card.name}")
            return LoadResult(
                success=False, model=card.name, location=card.location,
                error="Cannot connect to Ollama",
            )

        except Exception as e:
            log.error(f"Unloading {card.name} failed: {e}", exc_info=True)
            return LoadResult(
                success=False, model=card.name, location=card.location,
                error=str(e),
            )

    async def _rollback_evictions(self, evicted: list[ModelCard], gpu: GPU) -> None:
        """Re-load models that were evicted before a failed load.

        Best-effort: if rollback also fails, we log and move on.
        """
        if not evicted:
            return

        log.warning(f"Rolling back {len(evicted)} evictions on {gpu.name}")
        for card in evicted:
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    await client.post(
                        f"{self.ollama_url}/api/generate",
                        json={"model": card.name, "prompt": "", "keep_alive": "10m"},
                    )
                target = ModelLocation.GPU_0 if gpu == GPU.GPU_0 else ModelLocation.GPU_1
                self.registry.update_location(card.name, target)

                self._record_event(APUEvent(
                    event_type="rollback", model=card.name,
                    from_location="cpu_ram", to_location=target.value,
                    reason="Rolled back after failed load",
                ))
                log.info(f"Rollback: re-loaded {card.name} on {gpu.name}")

            except Exception as e:
                log.error(f"Rollback failed for {card.name}: {e} — model may be in inconsistent state")
                self._record_event(APUEvent(
                    event_type="error", model=card.name,
                    from_location="cpu_ram", to_location="unknown",
                    reason=f"Rollback failed: {e}",
                ))

    def _record_event(self, event: APUEvent) -> None:
        """Record an APU event for audit trail."""
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
