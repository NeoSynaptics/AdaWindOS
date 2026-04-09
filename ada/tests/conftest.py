"""Shared test fixtures for AdaOS tests."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from ada.apu.models import (
    ModelCard, ModelTier, ModelLocation, GPU, GPUSnapshot, HardwareSnapshot,
    VRAM_SAFETY_MARGIN_MB, GPU_VRAM_MB,
)
from ada.apu.registry import ModelRegistry
from ada.apu.monitor import HardwareMonitor


@pytest.fixture
def mock_monitor():
    """HardwareMonitor that returns configurable fake GPU data."""
    monitor = AsyncMock(spec=HardwareMonitor)

    # Default: GPU_0 has 10GB free, GPU_1 has 8GB free
    async def get_free(gpu):
        if gpu == GPU.GPU_0:
            return 10_000
        return 8_000

    monitor.get_gpu_free_mb = AsyncMock(side_effect=get_free)
    monitor.snapshot = AsyncMock(return_value=HardwareSnapshot(
        gpus=[
            GPUSnapshot(gpu=GPU.GPU_0, total_mb=16384, used_mb=6384, free_mb=10000),
            GPUSnapshot(gpu=GPU.GPU_1, total_mb=12288, used_mb=4288, free_mb=8000),
        ],
        ram_total_mb=131072, ram_used_mb=16000, ram_free_mb=115072,
    ))
    return monitor


@pytest.fixture
def registry(mock_monitor):
    """ModelRegistry with monitor attached and a few models registered."""
    reg = ModelRegistry()
    reg.set_monitor(mock_monitor)

    reg.register(ModelCard(
        name="whisper", tier=ModelTier.RESIDENT, vram_mb=2000, ram_mb=2000,
        preferred_gpu=GPU.GPU_1, location=ModelLocation.GPU_1,
        description="STT",
    ))
    reg.register(ModelCard(
        name="qwen3:14b", tier=ModelTier.VOICE, vram_mb=9000, ram_mb=9000,
        preferred_gpu=GPU.GPU_0,
        description="Voice brain",
    ))
    reg.register(ModelCard(
        name="qwen3:32b", tier=ModelTier.CODING, vram_mb=18000, ram_mb=18000,
        preferred_gpu=None,
        description="Coding model — FORGE tasks",
    ))
    reg.register(ModelCard(
        name="gemma4:27b", tier=ModelTier.CODING, vram_mb=16000, ram_mb=16000,
        preferred_gpu=GPU.GPU_0,
        description="Multimodal model — vision tasks",
    ))
    return reg


@pytest.fixture
def mock_store():
    """Mock Store with common async methods."""
    store = AsyncMock()
    store.pool = AsyncMock()
    store.search_memories = AsyncMock(return_value=[])
    store.insert_memory = AsyncMock(return_value="mem_001")
    store.insert_note = AsyncMock()
    store.update_note_status = AsyncMock()
    store.get_active_tasks = AsyncMock(return_value=[])
    store.get_active_notes = AsyncMock(return_value=[])
    return store
