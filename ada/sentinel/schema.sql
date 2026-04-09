-- Sentinel Error Intelligence Schema
-- Tracks error reports, patches, and their lifecycle.

-- Error reports (the "case files")
CREATE TABLE IF NOT EXISTS sentinel_reports (
    report_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity TEXT NOT NULL,              -- LOW, MEDIUM, HIGH, CRITICAL
    title TEXT NOT NULL,
    subsystem TEXT NOT NULL,             -- apu, memory, voice, executive, decision, etc.

    -- Error signature (dedup key)
    signature_key TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    module TEXT NOT NULL,
    function TEXT NOT NULL,
    message_hash TEXT NOT NULL,

    -- Diagnostics
    root_cause_hint TEXT,
    exception TEXT NOT NULL,
    traceback TEXT NOT NULL,
    locals_snapshot JSONB,

    -- System state at time of error
    voice_state TEXT,
    executive_state TEXT,
    overlay_state TEXT,
    recent_event_id TEXT,
    recent_input TEXT,
    active_task_ids TEXT[],

    -- Code context
    affected_files TEXT[],
    source_snippets JSONB,               -- file_path → source code
    fix_target_files TEXT[],
    fix_hint TEXT,

    -- History
    related_error_ids TEXT[],
    occurrence_count INTEGER DEFAULT 1,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,

    -- Resolution
    resolved BOOLEAN DEFAULT FALSE,
    resolution_patch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_sentinel_reports_sig ON sentinel_reports (signature_key);
CREATE INDEX IF NOT EXISTS idx_sentinel_reports_subsystem ON sentinel_reports (subsystem);
CREATE INDEX IF NOT EXISTS idx_sentinel_reports_unresolved ON sentinel_reports (resolved, severity)
    WHERE resolved = FALSE;
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

    -- Authorship
    author TEXT NOT NULL,                -- "llm:<model>" or "human:<name>"
    source_prompt TEXT,                  -- the prompt that generated this patch

    -- Validation gates
    verdict TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, APPROVED, REJECTED, APPLIED, ROLLED_BACK
    syntax_valid BOOLEAN DEFAULT FALSE,
    scope_valid BOOLEAN DEFAULT FALSE,
    sandbox_passed BOOLEAN DEFAULT FALSE,

    -- Approval + application
    approval_hash TEXT,                  -- user's confirmation token
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    rollback_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_sentinel_patches_verdict ON sentinel_patches (verdict);
CREATE INDEX IF NOT EXISTS idx_sentinel_patches_report ON sentinel_patches (target_report_id);


-- Diagnostics (user-triggered investigations via LLM-generated scripts)
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
    verdict TEXT NOT NULL DEFAULT 'INCONCLUSIVE',  -- PASS, WARN, FAIL, INCONCLUSIVE, HARNESS_BLOCKED
    findings TEXT[],
    measurements JSONB,
    recommendation TEXT,
    model_used TEXT
);

CREATE INDEX IF NOT EXISTS idx_sentinel_diagnostics_subsystem ON sentinel_diagnostics (target_subsystem);
CREATE INDEX IF NOT EXISTS idx_sentinel_diagnostics_verdict ON sentinel_diagnostics (verdict);
