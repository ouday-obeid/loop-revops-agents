-- Loop RevOps Agents — shared schema
-- Postgres-portable: JSON columns as TEXT, no AUTOINCREMENT, no dialect-specific features.

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium',
    category TEXT,
    source TEXT,
    assignee TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_agent_status ON tasks(agent_name, status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority, status);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    input TEXT,
    output TEXT,
    status TEXT NOT NULL,
    duration_ms INTEGER,
    tokens_used INTEGER,
    cost_usd REAL,
    error_message TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runs_agent_date ON agent_runs(agent_name, started_at);

CREATE TABLE IF NOT EXISTS approval_gates (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    justification TEXT,
    requested_by TEXT NOT NULL,
    approved_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    slack_message_ts TEXT,
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP,
    expires_at TIMESTAMP,
    cooldown_until TIMESTAMP,
    parent_gate_id INTEGER,
    FOREIGN KEY (parent_gate_id) REFERENCES approval_gates(id)
);

CREATE INDEX IF NOT EXISTS idx_gates_status ON approval_gates(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_gates_cooldown ON approval_gates(status, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_gates_parent ON approval_gates(parent_gate_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    before_value TEXT,
    after_value TEXT,
    approval_gate_id INTEGER,
    rate_limit_bucket TEXT,
    run_id INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (approval_gate_id) REFERENCES approval_gates(id),
    FOREIGN KEY (run_id) REFERENCES agent_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_audit_agent_time ON audit_log(agent_name, timestamp);

CREATE TABLE IF NOT EXISTS rate_limits (
    id INTEGER PRIMARY KEY,
    bucket TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    window_start TIMESTAMP NOT NULL,
    limit_value INTEGER NOT NULL,
    UNIQUE(bucket, window_start)
);

CREATE TABLE IF NOT EXISTS integration_health (
    id INTEGER PRIMARY KEY,
    integration TEXT NOT NULL,
    status TEXT NOT NULL,
    last_success TIMESTAMP,
    last_failure TIMESTAMP,
    error_message TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_health_integration ON integration_health(integration, checked_at);

CREATE TABLE IF NOT EXISTS describe_cache (
    id INTEGER PRIMARY KEY,
    org_alias TEXT NOT NULL,
    sobject TEXT NOT NULL,
    describe_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_alias, sobject)
);

CREATE INDEX IF NOT EXISTS idx_describe_age ON describe_cache(fetched_at);

-- CS agent tables (0003_cs_agent migration).
CREATE TABLE IF NOT EXISTS cs_account_health (
    account_id TEXT PRIMARY KEY,
    vitally_uid TEXT,
    name TEXT,
    score REAL,
    nps_score INTEGER,
    nps_category TEXT,
    nps_at TIMESTAMP,
    last_touch_at TIMESTAMP,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cs_health_checked ON cs_account_health(checked_at);
CREATE INDEX IF NOT EXISTS idx_cs_health_vitally ON cs_account_health(vitally_uid);

CREATE TABLE IF NOT EXISTS cs_account_health_history (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    score REAL,
    nps_score INTEGER,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cs_health_hist ON cs_account_health_history(account_id, checked_at);

CREATE TABLE IF NOT EXISTS cs_churn_risk (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    score INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    factors_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, created_at)
);

CREATE INDEX IF NOT EXISTS idx_cs_risk_tier ON cs_churn_risk(tier, created_at);
CREATE INDEX IF NOT EXISTS idx_cs_risk_account ON cs_churn_risk(account_id, created_at);

CREATE TABLE IF NOT EXISTS cs_renewal_state (
    opportunity_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    stage TEXT,
    contract_end_date DATE,
    last_activity_at TIMESTAMP,
    brief_sent_at TIMESTAMP,
    provisional INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cs_renewal_account ON cs_renewal_state(account_id);
CREATE INDEX IF NOT EXISTS idx_cs_renewal_end ON cs_renewal_state(contract_end_date);
