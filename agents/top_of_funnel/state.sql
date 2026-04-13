-- Top of Funnel local state. Separate from shared/db/schema.sql because these
-- tables are agent-internal (credit ledger, per-account cache, routing rotation,
-- lead candidate buffer) and should not be read by other specialists.
--
-- Engine: SQLite at agents/top_of_funnel/state.db (or resolved via
-- shared.db.connection.get_agent_engine('top_of_funnel') once that helper lands).

CREATE TABLE IF NOT EXISTS clay_credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,              -- 'YYYY-MM'
    consumed INTEGER NOT NULL DEFAULT 0,
    cap INTEGER NOT NULL,
    alerted_80pct_at TIMESTAMP,
    alerted_100pct_at TIMESTAMP,
    UNIQUE(month)
);

CREATE TABLE IF NOT EXISTS suppression_cache (
    email TEXT PRIMARY KEY,
    suppressed INTEGER NOT NULL,      -- 0 | 1
    reason TEXT,
    source TEXT,
    checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_suppression_cache_checked_at
    ON suppression_cache(checked_at);

CREATE TABLE IF NOT EXISTS tof_enrichment_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    status TEXT NOT NULL,             -- 'running' | 'success' | 'error' | 'partial'
    scanned INTEGER DEFAULT 0,
    suppressed INTEGER DEFAULT 0,
    scored_a INTEGER DEFAULT 0,
    scored_b INTEGER DEFAULT 0,
    written_count INTEGER DEFAULT 0,
    errors_json TEXT
);

CREATE TABLE IF NOT EXISTS tof_lead_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    domain TEXT NOT NULL,
    company_name TEXT,
    email TEXT,
    first_name TEXT,
    last_name TEXT,
    title TEXT,
    phone TEXT,
    location_count INTEGER,
    brand TEXT,
    ownership_type TEXT,
    icp_score INTEGER,
    icp_tier TEXT,                    -- 'A' | 'B' | 'C' | 'D'
    icp_signals_json TEXT,
    account_payload TEXT,             -- raw Apollo+Clay firmographics as JSON
    assigned_sdr_id TEXT,             -- SF User.Id
    sf_lead_id TEXT,                  -- populated after SF create
    status TEXT NOT NULL DEFAULT 'ready',   -- 'ready' | 'briefed' | 'enrolled' | 'suppressed' | 'error'
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    briefed_at TIMESTAMP,
    enrolled_at TIMESTAMP,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS ix_tof_lead_candidates_run_id
    ON tof_lead_candidates(run_id);
CREATE INDEX IF NOT EXISTS ix_tof_lead_candidates_status
    ON tof_lead_candidates(status);
CREATE INDEX IF NOT EXISTS ix_tof_lead_candidates_sdr
    ON tof_lead_candidates(assigned_sdr_id, status);

CREATE TABLE IF NOT EXISTS tof_routing_state (
    segment TEXT PRIMARY KEY,         -- 'ENT' | 'MM' | 'SMB'
    last_assigned_index INTEGER NOT NULL DEFAULT -1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS apollo_query_cache (
    query_hash TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    cached_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
