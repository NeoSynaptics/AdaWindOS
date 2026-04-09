"""Tests for APU ModelRegistry — VRAM bookkeeping, eviction, and real VRAM queries."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from ada.apu.models import (
    ModelCard, ModelTier, ModelLocation, GPU,
    GPU_VRAM_MB, VRAM_SAFETY_MARGIN_MB,
)
from ada.apu.registry import ModelRegistry


class TestBookkeepingVRAM:
    """Tests for the bookkeeping-based VRAM calculation."""

    def test_empty_gpu_has_full_capacity(self, registry):
        # GPU_0 has no models loaded on it
        free = registry.gpu_free_mb(GPU.GPU_0)
        assert free == GPU_VRAM_MB[GPU.GPU_0] - VRAM_SAFETY_MARGIN_MB

    def test_loaded_model_reduces_free(self, registry):
        # Load voice model on GPU_0
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        free = registry.gpu_free_mb(GPU.GPU_0)
        expected = GPU_VRAM_MB[GPU.GPU_0] - 9000 - VRAM_SAFETY_MARGIN_MB
        assert free == expected

    def test_resident_model_counted(self, registry):
        # Whisper is already on GPU_1
        free = registry.gpu_free_mb(GPU.GPU_1)
        expected = GPU_VRAM_MB[GPU.GPU_1] - 2000 - VRAM_SAFETY_MARGIN_MB
        assert free == expected

    def test_used_mb_sums_correctly(self, registry):
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        assert registry.gpu_used_mb(GPU.GPU_0) == 9000
        assert registry.gpu_used_mb(GPU.GPU_1) == 2000  # whisper


class TestRealVRAM:
    """Tests for the hardware-backed VRAM query."""

    @pytest.mark.asyncio
    async def test_real_vram_uses_monitor(self, registry, mock_monitor):
        # Monitor says GPU_0 has 10000 MB free
        free = await registry.gpu_free_mb_real(GPU.GPU_0)
        assert free == 10000 - VRAM_SAFETY_MARGIN_MB
        mock_monitor.get_gpu_free_mb.assert_called_with(GPU.GPU_0)

    @pytest.mark.asyncio
    async def test_real_vram_fallback_on_error(self, registry, mock_monitor):
        # Monitor raises — should fall back to bookkeeping
        mock_monitor.get_gpu_free_mb = AsyncMock(side_effect=RuntimeError("NVML gone"))
        free = await registry.gpu_free_mb_real(GPU.GPU_0)
        expected = registry.gpu_free_mb(GPU.GPU_0)
        assert free == expected

    @pytest.mark.asyncio
    async def test_real_vram_fallback_no_monitor(self):
        reg = ModelRegistry()  # no monitor set
        reg.register(ModelCard(
            name="test", tier=ModelTier.CODING, vram_mb=5000, ram_mb=5000,
            preferred_gpu=GPU.GPU_0, description="test",
        ))
        free = await reg.gpu_free_mb_real(GPU.GPU_0)
        assert free == reg.gpu_free_mb(GPU.GPU_0)


class TestEviction:
    """Tests for eviction candidate ordering."""

    def test_resident_never_evicted(self, registry):
        registry.update_location("whisper", ModelLocation.GPU_1)
        candidates = registry.eviction_candidates(GPU.GPU_1, 5000)
        names = [c.name for c in candidates]
        assert "whisper" not in names

    def test_eviction_order_by_tier(self, registry):
        # Load both voice and coding on GPU_0
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        registry.update_location("gemma4:27b", ModelLocation.GPU_0)
        candidates = registry.eviction_candidates(GPU.GPU_0, 30000)
        names = [c.name for c in candidates]
        # CODING (lower priority) should come before VOICE
        assert names.index("gemma4:27b") < names.index("qwen3:14b")

    def test_in_use_models_not_evicted(self, registry):
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        registry.acquire_ref("qwen3:14b")
        candidates = registry.eviction_candidates(GPU.GPU_0, 5000)
        names = [c.name for c in candidates]
        assert "qwen3:14b" not in names

    def test_can_fit(self, registry):
        # GPU_0 is empty (except for models on disk)
        assert registry.can_fit("qwen3:14b", GPU.GPU_0)  # 9000 fits in 16384

    def test_cannot_fit_too_large(self, registry):
        # Load voice on GPU_0, then coding won't fit (9000 + 15000 > 16384)
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        assert not registry.can_fit("gemma4:27b", GPU.GPU_0)


class TestRefCounting:
    """Tests for reference counting."""

    def test_acquire_release(self, registry):
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        assert registry.acquire_ref("qwen3:14b")
        card = registry.get("qwen3:14b")
        assert card.ref_count == 1
        assert card.is_in_use

        registry.release_ref("qwen3:14b")
        assert card.ref_count == 0
        assert not card.is_in_use

    def test_acquire_fails_if_not_on_gpu(self, registry):
        assert not registry.acquire_ref("qwen3:14b")  # still on disk

    def test_release_never_goes_negative(self, registry):
        registry.update_location("qwen3:14b", ModelLocation.GPU_0)
        registry.release_ref("qwen3:14b")
        card = registry.get("qwen3:14b")
        assert card.ref_count == 0
