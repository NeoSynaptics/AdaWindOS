"""AdaWindOS configuration — cloud-first, no GPU required.

All model inference goes through DeepSeek (or any OpenAI-compatible API).
No Ollama, no local models, no VRAM management.
"""

import os
from dataclasses import dataclass, field


@dataclass
class APUConfig:
    """APU — disabled on Windows. Cloud gateway handles everything."""
    enabled: bool = False


@dataclass
class ModelConfig:
    # Cloud model for all tasks (DeepSeek V3.2)
    control_model: str = "deepseek-chat"
    coding_model: str = "deepseek-chat"
    multimodal_model: str = ""

    # Embeddings — CPU, local (small model, works on any laptop)
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384

    # Worker model (same cloud API)
    worker_fallback_model: str = "deepseek-chat"
    worker_fallback_base: str = "https://api.deepseek.com/v1"


@dataclass
class CloudConfig:
    """Cloud LLM API configuration."""
    api_base: str = "https://api.deepseek.com/v1"
    api_key: str = os.environ.get("DEEPSEEK_API_KEY", "")
    default_model: str = "deepseek-chat"


@dataclass
class TTSConfig:
    primary: str = "none"
    fallback: str = "none"


@dataclass
class TRIBEConfig:
    enabled_background: bool = False
    disabled_during_live_turns: bool = True
    mode: str = "critic"
    proactive_speech_requires_tribe: bool = False
    interrupt_threshold: float = 0.6
    speak_now_threshold: float = 0.5
    candidate_count: int = 1


@dataclass
class BudgetConfig:
    monthly_budget_usd: float = 100.0
    daily_soft_cap_usd: float = 3.30
    per_task_token_cap: int = 100_000
    max_retries: int = 3
    max_dispatches_per_hour: int = 5
    max_concurrent_tasks: int = 10
    dispatch_cooldown_sec: int = 30


@dataclass
class DatabaseConfig:
    """SQLite — fields kept for API compat."""
    host: str = "localhost"
    port: int = 0
    database: str = "ada"
    user: str = ""
    password: str = ""
    min_pool_size: int = 1
    max_pool_size: int = 1


@dataclass
class OllamaConfig:
    """Disabled — kept for import compatibility."""
    base_url: str = ""
    api_url: str = ""


@dataclass
class VoiceConfig:
    enabled: bool = False
    stt_model: str = ""
    wake_word: str = "hey ada"
    attentive_timeout_sec: int = 120
    ack_deadline_ms: int = 1000
    input_device_index: int | None = None
    output_device_index: int | None = None


@dataclass
class LearningConfig:
    """LoRA training — disabled without GPU."""
    enabled: bool = False


@dataclass
class ContextBudgetConfig:
    chars_per_token: int = 4
    l0_identity_tokens: int = 800
    l1_hot_tokens: int = 2000
    l2_memory_tokens: int = 1500
    l3_deep_tokens: int = 700

    @property
    def total_budget_tokens(self) -> int:
        return (self.l0_identity_tokens + self.l1_hot_tokens
                + self.l2_memory_tokens + self.l3_deep_tokens)


@dataclass
class MCPConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6920


@dataclass
class GooseConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 3199
    bin_path: str = ""


@dataclass
class WorldMonitorConfig:
    enabled: bool = False
    base_url: str = ""
    public_url: str = ""
    api_key: str = ""
    use_local: bool = False
    intelligence_poll_sec: int = 300
    market_poll_sec: int = 60
    conflict_poll_sec: int = 600
    news_poll_sec: int = 300
    risk_score_alert_threshold: float = 7.0
    escalation_alert: bool = False
    market_move_pct: float = 3.0
    watched_countries: list = field(default_factory=list)
    watched_sectors: list = field(default_factory=list)
    morning_briefing_hour: int = 8
    evening_briefing_hour: int = 20


@dataclass
class SentinelConfig:
    enabled: bool = False
    dedup_window_sec: int = 60
    max_reports_per_hour: int = 50
    sandbox_timeout_sec: int = 10
    auto_export_markdown: bool = False
    export_dir: str = "sentinel_reports"


@dataclass
class AdaConfig:
    apu: APUConfig = field(default_factory=APUConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    tribe: TRIBEConfig = field(default_factory=TRIBEConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    sentinel: SentinelConfig = field(default_factory=SentinelConfig)
    context_budget: ContextBudgetConfig = field(default_factory=ContextBudgetConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    goose: GooseConfig = field(default_factory=GooseConfig)
    worldmonitor: WorldMonitorConfig = field(default_factory=WorldMonitorConfig)
