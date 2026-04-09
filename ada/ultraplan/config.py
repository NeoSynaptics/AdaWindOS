"""
UltraPlan configuration.

TODO: Implement dataclass-based config with env overrides.
"""

from dataclasses import dataclass, field


@dataclass
class UltraPlanConfig:
    # Model selection — bigger model at night since GPUs are free
    planning_model: str = "qwen3:72b"  # TODO: test Q4 vs Q5 quant on dual GPU
    critique_model: str = "qwen3:72b"  # same model for self-critique
    fallback_model: str = "qwen3:14b"  # if 72B fails to load

    # Ollama connection
    ollama_host: str = "http://localhost:11434"

    # GPU strategy
    # Daytime: GPU 0 (5060 Ti) = Qwen 14B, GPU 1 (4070) = Whisper
    # Overnight: both free for planning
    gpu_ids: list[int] = field(default_factory=lambda: [0, 1])

    # Planning passes per task
    max_passes: int = 3  # decompose → critique → synthesize
    extra_refinement_passes: int = 0  # optional additional critique rounds

    # Timing
    overnight_start_hour: int = 23  # 11 PM — daemon activates
    overnight_end_hour: int = 7    # 7 AM — daemon yields GPUs back to voice
    max_minutes_per_task: int = 60  # hard cap per planning task
    cooldown_between_tasks_sec: int = 30

    # Queue
    max_queued_tasks: int = 20
    max_tasks_per_night: int = 8  # realistic cap given ~50 min per task

    # Output
    plans_dir: str = "~/.ada/plans"  # where completed plans are written
    persist_intermediate: bool = True  # keep decompose + critique passes

    # Integration
    notify_ada_on_complete: bool = True  # Ada mentions plans in morning greeting
    push_to_noteboard: bool = True  # completed plans appear on noteboard


# TODO: Load from ~/.ada/ultraplan.yaml with env overrides
# TODO: Validate GPU availability at daemon start (nvidia-smi check)
# TODO: Auto-detect model fit (VRAM check before loading 72B)
