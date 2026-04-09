"""APU data models — model cards, GPU enums, hardware snapshots."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any


class ModelTier(Enum):
    """Priority tiers — higher priority = harder to evict."""
    RESIDENT = auto()       # P0: never evicted (Whisper STT)
    VOICE = auto()          # P1: Ada's brain, evicted only when idle
    CODING = auto()         # P2: background coding, yields to voice
    WARM = auto()           # P3: in RAM, promote to GPU in seconds
    COLD = auto()           # P4: on disk, needs full load
    PLANNING = auto()       # P5: overnight only, takes both GPUs


class ModelLocation(Enum):
    """Where a model currently lives."""
    GPU_0 = "gpu_0"                 # RTX 5060 Ti (16GB)
    GPU_1 = "gpu_1"                 # RTX 4070 (12GB)
    GPU_SPLIT = "gpu_split"         # Split across both GPUs
    CPU_RAM = "cpu_ram"             # System RAM (128GB)
    DISK = "disk"                   # On disk, not loaded
    NOT_AVAILABLE = "not_available" # Not pulled/installed


class GPU(Enum):
    """Physical GPUs."""
    GPU_0 = 0   # RTX 5060 Ti, 16GB
    GPU_1 = 1   # RTX 4070, 12GB


# GPU VRAM capacities in MB
GPU_VRAM_MB = {
    GPU.GPU_0: 16_384,  # 16GB
    GPU.GPU_1: 12_288,  # 12GB
}

VRAM_SAFETY_MARGIN_MB = 512  # Keep 512MB free for overhead


@dataclass
class ModelCard:
    """Metadata for a model managed by the APU."""
    name: str                           # Ollama model name (e.g. "gpt-oss:20b")
    tier: ModelTier                     # Priority tier
    vram_mb: int                        # VRAM needed when loaded on GPU
    ram_mb: int                         # RAM needed when loaded in CPU memory
    preferred_gpu: GPU | None = None    # Which GPU it prefers (None = any)
    location: ModelLocation = ModelLocation.DISK
    last_used: datetime | None = None
    ref_count: int = 0                  # Active inference references (don't evict if > 0)
    load_time_sec: float = 0.0          # How long last load took
    description: str = ""

    @property
    def is_on_gpu(self) -> bool:
        return self.location in (ModelLocation.GPU_0, ModelLocation.GPU_1, ModelLocation.GPU_SPLIT)

    @property
    def is_loaded(self) -> bool:
        return self.location in (ModelLocation.GPU_0, ModelLocation.GPU_1,
                                  ModelLocation.GPU_SPLIT, ModelLocation.CPU_RAM)

    @property
    def is_in_use(self) -> bool:
        return self.ref_count > 0

    def touch(self) -> None:
        self.last_used = datetime.now()


@dataclass
class GPUSnapshot:
    """Current state of one GPU."""
    gpu: GPU
    total_mb: int
    used_mb: int
    free_mb: int
    temperature_c: int = 0
    utilization_pct: int = 0
    models_loaded: list[str] = field(default_factory=list)


@dataclass
class HardwareSnapshot:
    """Full hardware state at a point in time."""
    gpus: list[GPUSnapshot]
    ram_total_mb: int = 0
    ram_used_mb: int = 0
    ram_free_mb: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    def gpu(self, g: GPU) -> GPUSnapshot | None:
        for snap in self.gpus:
            if snap.gpu == g:
                return snap
        return None

    def gpu_free_mb(self, g: GPU) -> int:
        snap = self.gpu(g)
        return snap.free_mb if snap else 0


@dataclass
class LoadResult:
    """Result of a model load/unload operation."""
    success: bool
    model: str
    location: ModelLocation
    duration_sec: float = 0.0
    error: str | None = None
    evicted: list[str] = field(default_factory=list)  # models that were evicted to make room


@dataclass
class APUEvent:
    """Structured event for audit trail."""
    event_type: str     # load | unload | evict | promote | demote | error | thrash | rollback
    model: str
    from_location: str
    to_location: str
    vram_before_mb: int = 0
    vram_after_mb: int = 0
    duration_sec: float = 0.0
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
