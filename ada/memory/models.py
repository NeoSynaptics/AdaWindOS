"""Data models for all persistent objects in AdaOS."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# --- Task ---

@dataclass
class Task:
    task_id: str
    title: str
    type: str           # architecture | implementation | research | validation | note_followup | system
    origin: str         # voice | noteboard | scheduled | conditional | agent_followup | manual
    status: str = "draft"   # draft | queued | dispatched | in_progress | awaiting_result | awaiting_user | validating | done | failed | cancelled
    priority: str = "medium"
    visibility: str = "ada_private"
    owner: str = "ADA"
    created_by: str = "ADA"
    brief: str = ""
    success_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    artifacts_expected: list[str] = field(default_factory=list)
    artifacts_received: list[dict] = field(default_factory=list)
    linked_notes: list[str] = field(default_factory=list)
    linked_tasks: list[str] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    budget_class: str = "free"
    cloud_budget_limit: int = 0
    requires_user_decision: bool = False
    escalation_reason: str | None = None
    dispatch_target: str | None = None
    repo_path: str | None = None        # FORGE: real repo path for grounded coding
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None


# --- Note ---

@dataclass
class Note:
    note_id: str
    content: str
    type: str           # personal | actionable | scheduled | conditional
    status: str = "captured"    # captured | pinned | candidate_task | promoted_to_task | archived
    priority: str = "low"
    source: str = "voice"       # voice | noteboard_ui | agent_result | auto_summary
    owner: str = "USER"
    visibility: str = "personal"
    linked_task_id: str | None = None
    confidence: float = 1.0
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


# --- Episode (episodic memory — what happened) ---

@dataclass
class Episode:
    session_id: str
    turn_type: str      # user | ada | agent | system
    speaker: str
    content: str
    embedding: list[float] | None = None
    decision_event_id: str | None = None
    consolidated: bool = False
    timestamp: datetime = field(default_factory=datetime.now)


# --- Memory (semantic memory — extracted facts) ---

@dataclass
class Memory:
    content: str
    memory_type: str    # fact | preference | decision | pattern | correction
    confidence: float = 1.0
    source_episode_ids: list[int] = field(default_factory=list)
    embedding: list[float] | None = None
    valid_from: datetime = field(default_factory=datetime.now)
    valid_until: datetime | None = None     # None = still valid


# --- Entity + Relation (lightweight graph) ---

@dataclass
class Entity:
    name: str
    entity_type: str    # project | person | technology | concept | agent
    embedding: list[float] | None = None


@dataclass
class Relation:
    subject_id: int
    predicate: str      # uses | depends_on | contradicts | prefers | owns
    object_id: int
    confidence: float = 1.0
    valid_from: datetime = field(default_factory=datetime.now)
    valid_until: datetime | None = None
    source_memory_id: int | None = None


# --- Journal ---

@dataclass
class JournalEntry:
    action_type: str    # dispatch | validate | retry | file_edit | command | note | state_change | budget_spend | escalation
    action_summary: str
    task_id: str | None = None
    agent: str | None = None
    band: int = 1
    budget_impact: dict[str, Any] = field(default_factory=dict)
    rollback_hint: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


# --- Outbox ---

@dataclass
class OutboxEvent:
    decision_event_id: str
    event_type: str     # agent.dispatch | noteboard.push | delivery.queue | tts.speak
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"     # pending | processing | processed | failed
    attempt: int = 0
    max_attempts: int = 5
    error: str | None = None
    visible_at: datetime = field(default_factory=datetime.now)
    processed_at: datetime | None = None


# --- Session ---

@dataclass
class Session:
    session_id: str
    started_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    turn_count: int = 0
