"""Model registry — fleet management with tier-aware eviction ordering.

Tracks all models the APU knows about: where they are, how much VRAM they need,
what priority they have, and in what order they should be evicted.
"""

import logging
from datetime import datetime

from .models import ModelCard, ModelTier, ModelLocation, GPU, GPU_VRAM_MB, VRAM_SAFETY_MARGIN_MB

log = logging.getLogger("ada.apu.registry")

# Eviction order: COLD first, then WARM, then CODING, then VOICE. RESIDENT never.
EVICTION_ORDER = [
    ModelTier.COLD,
    ModelTier.WARM,
    ModelTier.PLANNING,
    ModelTier.CODING,
    ModelTier.VOICE,
    # ModelTier.RESIDENT is never evicted
]


class ModelRegistry:
    """Manages the fleet of models and their locations."""

    def __init__(self):
        self._models: dict[str, ModelCard] = {}
        self._gpu_usage: dict[GPU, int] = {GPU.GPU_0: 0, GPU.GPU_1: 0}
        self._monitor = None  # HardwareMonitor, set via set_monitor()

    def set_monitor(self, monitor) -> None:
        """Attach a HardwareMonitor for real VRAM queries."""
        self._monitor = monitor

    def register(self, card: ModelCard) -> None:
        """Register a model with the APU."""
        if card.name in self._models:
            log.warning(f"Re-registering model {card.name} — overwriting previous card")
        self._models[card.name] = card
        log.info(f"Registered model: {card.name} (tier={card.tier.name}, vram={card.vram_mb}MB)")

    def get(self, name: str) -> ModelCard | None:
        return self._models.get(name)

    def all_models(self) -> list[ModelCard]:
        return list(self._models.values())

    def models_on_gpu(self, gpu: GPU) -> list[ModelCard]:
        """Get all models currently on a specific GPU."""
        loc = ModelLocation.GPU_0 if gpu == GPU.GPU_0 else ModelLocation.GPU_1
        return [m for m in self._models.values() if m.location == loc]

    def models_by_tier(self, tier: ModelTier) -> list[ModelCard]:
        return [m for m in self._models.values() if m.tier == tier]

    def gpu_used_mb(self, gpu: GPU) -> int:
        """Total VRAM used on a GPU by tracked models."""
        loc = ModelLocation.GPU_0 if gpu == GPU.GPU_0 else ModelLocation.GPU_1
        return sum(m.vram_mb for m in self._models.values() if m.location == loc)

    def gpu_free_mb(self, gpu: GPU) -> int:
        """Available VRAM on a GPU via bookkeeping (capacity - used - safety margin).

        For load decisions, prefer gpu_free_mb_real() which queries actual hardware.
        """
        total = GPU_VRAM_MB[gpu]
        used = self.gpu_used_mb(gpu)
        return max(0, total - used - VRAM_SAFETY_MARGIN_MB)

    async def gpu_free_mb_real(self, gpu: GPU) -> int:
        """Available VRAM on a GPU from actual hardware query via pynvml.

        Falls back to bookkeeping if monitor is unavailable or query fails.
        This is the method that should be used for load/eviction decisions,
        since it reflects reality (other processes, fragmentation, Ollama overhead).
        """
        if self._monitor is not None:
            try:
                real_free = await self._monitor.get_gpu_free_mb(gpu)
                return max(0, real_free - VRAM_SAFETY_MARGIN_MB)
            except Exception as e:
                log.warning(f"Real VRAM query failed for {gpu.name}: {e} — falling back to bookkeeping")
        return self.gpu_free_mb(gpu)

    def can_fit(self, model_name: str, gpu: GPU) -> bool:
        """Check if a model fits on a GPU without eviction."""
        card = self.get(model_name)
        if not card:
            log.error(f"can_fit: unknown model {model_name}")
            return False
        return card.vram_mb <= self.gpu_free_mb(gpu)

    def eviction_candidates(self, gpu: GPU, needed_mb: int) -> list[ModelCard]:
        """Get models to evict from a GPU to free needed_mb, ordered by eviction priority.

        Rules:
        - Never evict RESIDENT tier
        - Never evict models with ref_count > 0 (in-use)
        - Evict lowest priority first (COLD → WARM → PLANNING → CODING → VOICE)
        - Within same tier, evict least recently used first
        """
        loc = ModelLocation.GPU_0 if gpu == GPU.GPU_0 else ModelLocation.GPU_1
        candidates = []

        for tier in EVICTION_ORDER:
            tier_models = [
                m for m in self._models.values()
                if m.location == loc and m.tier == tier and not m.is_in_use
            ]
            # Sort by last_used ascending (oldest first)
            tier_models.sort(key=lambda m: m.last_used or datetime.min)
            candidates.extend(tier_models)

        # Calculate how many we need to evict
        result = []
        freed = 0
        for model in candidates:
            if freed >= needed_mb:
                break
            result.append(model)
            freed += model.vram_mb

        if freed < needed_mb:
            log.warning(
                f"Cannot free enough VRAM on {gpu.name}: need {needed_mb}MB, "
                f"can free {freed}MB (remaining models are RESIDENT or in-use)"
            )

        return result

    def update_location(self, model_name: str, location: ModelLocation) -> None:
        """Update a model's location after load/unload."""
        card = self.get(model_name)
        if not card:
            log.error(f"update_location: unknown model {model_name}")
            return

        old = card.location
        card.location = location
        card.touch()
        log.debug(f"Model {model_name}: {old.value} → {location.value}")

    def acquire_ref(self, model_name: str) -> bool:
        """Increment ref count (model is being used for inference). Returns False if not loaded."""
        card = self.get(model_name)
        if not card:
            log.error(f"acquire_ref: unknown model {model_name}")
            return False
        if not card.is_on_gpu:
            log.warning(f"acquire_ref: {model_name} not on GPU (location={card.location.value})")
            return False
        card.ref_count += 1
        card.touch()
        return True

    def release_ref(self, model_name: str) -> None:
        """Decrement ref count after inference completes."""
        card = self.get(model_name)
        if not card:
            return
        card.ref_count = max(0, card.ref_count - 1)

    def status_summary(self) -> dict:
        """Human-readable status for health checks."""
        return {
            "models": {
                m.name: {
                    "tier": m.tier.name,
                    "location": m.location.value,
                    "vram_mb": m.vram_mb,
                    "ref_count": m.ref_count,
                    "last_used": m.last_used.isoformat() if m.last_used else None,
                }
                for m in self._models.values()
            },
            "gpu_0": {
                "used_mb": self.gpu_used_mb(GPU.GPU_0),
                "free_mb": self.gpu_free_mb(GPU.GPU_0),
                "total_mb": GPU_VRAM_MB[GPU.GPU_0],
            },
            "gpu_1": {
                "used_mb": self.gpu_used_mb(GPU.GPU_1),
                "free_mb": self.gpu_free_mb(GPU.GPU_1),
                "total_mb": GPU_VRAM_MB[GPU.GPU_1],
            },
        }
