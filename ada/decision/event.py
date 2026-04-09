"""Decision Event — the atomic unit of AdaOS."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Source:
    type: str           # user_voice | agent_result | timer | system | noteboard_ui | startup
    raw_input: str
    context_refs: list[str] = field(default_factory=list)
    turn_id: str | None = None


@dataclass
class Classification:
    intent: str         # one of 15 closed-set intents
    confidence: float
    target_agent: str | None = None     # ARCH | FORGE | None
    urgency: str = "low"                # low | medium | high
    note_action: str = "none"           # create | update | none
    topic: str = ""
    references_existing: bool = False
    reasoning_trace: str | None = None  # optional debug info


@dataclass
class Action:
    action: str         # one of 16 closed-set actions
    band: int = 1       # policy band: 1=free, 2=logged, 3=escalate
    target: str | None = None
    requires_confirmation: bool = False
    reason: str | None = None


@dataclass
class ToolCallResult:
    tool: str
    params: dict[str, Any]
    result: dict[str, Any] | None = None
    success: bool = True
    duration_ms: int = 0


@dataclass
class Execution:
    tool_calls: list[ToolCallResult] = field(default_factory=list)
    state_transitions: list[dict[str, str]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)


@dataclass
class Budget:
    local_tokens_used: int = 0
    cloud_tokens_used: int = 0
    cumulative_cloud_today: int = 0


@dataclass
class Meta:
    processing_time_ms: int = 0
    model_used: str = ""
    latency_to_first_audio_ms: int = 0


@dataclass
class DecisionEvent:
    event_id: str
    timestamp: datetime
    sequence_num: int
    source: Source
    classification: Classification
    decision: Action
    execution: Execution = field(default_factory=Execution)
    budget: Budget = field(default_factory=Budget)
    meta: Meta = field(default_factory=Meta)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for PostgreSQL JSONB storage."""
        from dataclasses import asdict
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d
