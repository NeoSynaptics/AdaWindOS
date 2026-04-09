-- AdaOS PostgreSQL Schema

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Episodic memory (TimescaleDB hypertable)
CREATE TABLE IF NOT EXISTS episodes (
    id BIGSERIAL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id TEXT NOT NULL,
    turn_type TEXT NOT NULL,
    speaker TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(384),
    decision_event_id TEXT,
    consolidated BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('episodes', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_episodes_embedding ON episodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes (session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_episodes_unconsolidated ON episodes (consolidated) WHERE consolidated = FALSE;

-- Semantic memory (extracted facts)
CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    confidence FLOAT DEFAULT 1.0,
    source_episode_ids BIGINT[],
    embedding VECTOR(384) NOT NULL,
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memories_current ON memories (valid_until) WHERE valid_until IS NULL;

-- Entity-relationship graph
CREATE TABLE IF NOT EXISTS entities (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,
    embedding VECTOR(384),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS relations (
    id SERIAL PRIMARY KEY,
    subject_id INTEGER REFERENCES entities(id),
    predicate TEXT NOT NULL,
    object_id INTEGER REFERENCES entities(id),
    confidence FLOAT DEFAULT 1.0,
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_until TIMESTAMPTZ,
    source_memory_id INTEGER REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations (subject_id);
CREATE INDEX IF NOT EXISTS idx_relations_object ON relations (object_id);

-- Tasks
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    priority TEXT NOT NULL DEFAULT 'medium',
    visibility TEXT NOT NULL DEFAULT 'ada_private',
    owner TEXT NOT NULL DEFAULT 'ADA',
    created_by TEXT NOT NULL,
    brief TEXT,
    success_criteria JSONB,
    constraints JSONB,
    artifacts_expected JSONB,
    artifacts_received JSONB,
    linked_notes JSONB,
    linked_tasks JSONB,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    budget_class TEXT DEFAULT 'free',
    cloud_budget_limit INTEGER DEFAULT 0,
    requires_user_decision BOOLEAN DEFAULT FALSE,
    escalation_reason TEXT,
    dispatch_target TEXT,
    repo_path TEXT,                     -- FORGE: real repo path for grounded coding
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (owner);

-- Notes
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'captured',
    priority TEXT NOT NULL DEFAULT 'low',
    source TEXT NOT NULL DEFAULT 'voice',
    owner TEXT NOT NULL DEFAULT 'USER',
    visibility TEXT NOT NULL DEFAULT 'personal',
    linked_task_id TEXT REFERENCES tasks(task_id),
    confidence FLOAT DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Decision Events (the atom)
CREATE TABLE IF NOT EXISTS decision_events (
    event_id TEXT PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    sequence_num INTEGER NOT NULL,
    source JSONB NOT NULL,
    classification JSONB NOT NULL,
    decision JSONB NOT NULL,
    execution JSONB,
    budget JSONB,
    meta JSONB
);
CREATE INDEX IF NOT EXISTS idx_decision_events_ts ON decision_events (timestamp);

-- Journal (append-only execution ledger)
CREATE TABLE IF NOT EXISTS journal (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action_type TEXT NOT NULL,
    action_summary TEXT NOT NULL,
    task_id TEXT,
    agent TEXT,
    band INTEGER DEFAULT 1,
    budget_impact JSONB,
    rollback_hint TEXT,
    state_before TEXT,
    state_after TEXT,
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal (timestamp);
CREATE INDEX IF NOT EXISTS idx_journal_task ON journal (task_id);

-- Outbox (restart-safe side effect delivery)
CREATE TABLE IF NOT EXISTS outbox_events (
    id BIGSERIAL PRIMARY KEY,
    decision_event_id TEXT,  -- nullable: delivery/retry outbox events don't always have a decision event
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT DEFAULT 'pending',
    attempt INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    visible_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events (status, visible_at) WHERE status = 'pending';

-- Agent runs (tracks dispatched work)
CREATE TABLE IF NOT EXISTS agent_runs (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id),
    agent TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    workspace_path TEXT,
    request_hash TEXT,
    result_hash TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    turn_count INTEGER DEFAULT 0
);

-- UltraPlan queue
CREATE TABLE IF NOT EXISTS ultraplan_queue (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    domain TEXT DEFAULT 'general',
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'submitted',
    linked_task_id TEXT,
    submitted_at TIMESTAMPTZ DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    passes_completed INTEGER DEFAULT 0,
    final_plan TEXT,
    intermediate_outputs JSONB,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2
);

-- ============================================================
-- Sentinel — Error Intelligence System
-- ============================================================

-- Error reports (the "case files")
CREATE TABLE IF NOT EXISTS sentinel_reports (
    report_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    signature_key TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    module TEXT NOT NULL,
    function TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    root_cause_hint TEXT,
    exception TEXT NOT NULL,
    traceback TEXT NOT NULL,
    locals_snapshot JSONB,
    voice_state TEXT,
    executive_state TEXT,
    overlay_state TEXT,
    recent_event_id TEXT,
    recent_input TEXT,
    active_task_ids TEXT[],
    affected_files TEXT[],
    source_snippets JSONB,
    fix_target_files TEXT[],
    fix_hint TEXT,
    related_error_ids TEXT[],
    occurrence_count INTEGER DEFAULT 1,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    resolved BOOLEAN DEFAULT FALSE,
    resolution_patch_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sentinel_reports_sig ON sentinel_reports (signature_key);
CREATE INDEX IF NOT EXISTS idx_sentinel_reports_subsystem ON sentinel_reports (subsystem);
CREATE INDEX IF NOT EXISTS idx_sentinel_reports_unresolved ON sentinel_reports (resolved, severity) WHERE resolved = FALSE;
CREATE INDEX IF NOT EXISTS idx_sentinel_reports_last_seen ON sentinel_reports (last_seen DESC);

-- Patches (the controlled input gate)
CREATE TABLE IF NOT EXISTS sentinel_patches (
    patch_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_report_id TEXT NOT NULL REFERENCES sentinel_reports(report_id),
    description TEXT NOT NULL,
    diff TEXT NOT NULL,
    affected_files TEXT[],
    author TEXT NOT NULL,
    source_prompt TEXT,
    verdict TEXT NOT NULL DEFAULT 'PENDING',
    syntax_valid BOOLEAN DEFAULT FALSE,
    scope_valid BOOLEAN DEFAULT FALSE,
    sandbox_passed BOOLEAN DEFAULT FALSE,
    approval_hash TEXT,
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    rollback_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sentinel_patches_verdict ON sentinel_patches (verdict);
CREATE INDEX IF NOT EXISTS idx_sentinel_patches_report ON sentinel_patches (target_report_id);

-- Diagnostics (user-triggered investigations)
CREATE TABLE IF NOT EXISTS sentinel_diagnostics (
    diagnostic_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_question TEXT NOT NULL,
    target_subsystem TEXT NOT NULL,
    script_source TEXT NOT NULL,
    harness_verdict TEXT NOT NULL,
    harness_violations TEXT[],
    executed BOOLEAN DEFAULT FALSE,
    execution_stdout TEXT,
    execution_stderr TEXT,
    execution_time_ms INTEGER DEFAULT 0,
    execution_exit_code INTEGER DEFAULT -1,
    verdict TEXT NOT NULL DEFAULT 'INCONCLUSIVE',
    findings TEXT[],
    measurements JSONB,
    recommendation TEXT,
    model_used TEXT
);
CREATE INDEX IF NOT EXISTS idx_sentinel_diagnostics_subsystem ON sentinel_diagnostics (target_subsystem);
CREATE INDEX IF NOT EXISTS idx_sentinel_diagnostics_verdict ON sentinel_diagnostics (verdict);
