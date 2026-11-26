-- MiniMLflow schema (SQLite).
-- Design notes:
--   * IDs are application-generated 32-hex strings (run_id/trace_id) or
--     auto-increment integers serialized as strings (experiment_id/version).
--   * Soft deletion is used for experiments and runs so that accidental
--     deletion can be reversed and audit trails survive.
--   * Metric history is append-only. Search reads the latest row per key.
--   * All foreign keys cascade. SQLite requires PRAGMA foreign_keys=ON.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    artifact_location TEXT NOT NULL,
    lifecycle_stage   TEXT NOT NULL CHECK (lifecycle_stage IN ('active', 'deleted')),
    creation_time     INTEGER NOT NULL,
    last_update_time  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_tags (
    experiment_id INTEGER NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    PRIMARY KEY (experiment_id, key),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    experiment_id   INTEGER NOT NULL,
    run_name        TEXT NOT NULL DEFAULT '',
    user_id         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL CHECK (status IN ('RUNNING', 'SCHEDULED', 'FINISHED', 'FAILED', 'KILLED')),
    start_time      INTEGER NOT NULL,
    end_time        INTEGER,
    artifact_uri    TEXT NOT NULL,
    lifecycle_stage TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle_stage IN ('active', 'deleted')),
    deleted_time    INTEGER,
    deleted_with_experiment INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_experiment_status_time
    ON runs(experiment_id, status, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_runs_lifecycle
    ON runs(lifecycle_stage);

CREATE TABLE IF NOT EXISTS run_params (
    run_id TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (run_id, key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_params_key_value ON run_params(key, value);

CREATE TABLE IF NOT EXISTS run_tags (
    run_id TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (run_id, key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_tags_key_value ON run_tags(key, value);

CREATE TABLE IF NOT EXISTS metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     REAL NOT NULL,
    timestamp INTEGER NOT NULL,
    step      INTEGER NOT NULL DEFAULT 0 CHECK (step >= 0),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_run_key_latest
    ON metrics(run_id, key, timestamp DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_key_value ON metrics(key, value);

CREATE TABLE IF NOT EXISTS run_inputs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL,
    dataset_name       TEXT NOT NULL,
    dataset_digest     TEXT NOT NULL DEFAULT '',
    dataset_source     TEXT NOT NULL DEFAULT '',
    dataset_source_type TEXT NOT NULL DEFAULT '',
    context            TEXT NOT NULL DEFAULT 'training',
    tags_json          TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_inputs_run ON run_inputs(run_id);
-- Covers correlated dataset filters: EXISTS (SELECT 1 FROM run_inputs
-- WHERE run_id = r.run_id AND dataset_name/digest/context = ?).
CREATE INDEX IF NOT EXISTS idx_run_inputs_dataset
    ON run_inputs(run_id, dataset_name, dataset_digest, context);

CREATE TABLE IF NOT EXISTS registered_models (
    name                 TEXT PRIMARY KEY,
    creation_timestamp   INTEGER NOT NULL,
    last_updated_timestamp INTEGER NOT NULL,
    description          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS registered_model_tags (
    name  TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (name, key),
    FOREIGN KEY (name) REFERENCES registered_models(name) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS model_versions (
    name                 TEXT NOT NULL,
    version              INTEGER NOT NULL,
    creation_timestamp   INTEGER NOT NULL,
    last_updated_timestamp INTEGER NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    source               TEXT NOT NULL,
    run_id               TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'READY_REGISTRATION'
                             CHECK (status IN ('PENDING_REGISTRATION', 'READY', 'FAILED_REGISTRATION', 'READY_REGISTRATION')),
    current_stage        TEXT NOT NULL DEFAULT 'None',
    PRIMARY KEY (name, version),
    FOREIGN KEY (name) REFERENCES registered_models(name) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS model_version_tags (
    name    TEXT NOT NULL,
    version INTEGER NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (name, version, key),
    FOREIGN KEY (name, version) REFERENCES model_versions(name, version) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS model_version_aliases (
    name    TEXT NOT NULL,
    alias   TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (name, alias),
    FOREIGN KEY (name, version) REFERENCES model_versions(name, version) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_versions_name_time
    ON model_versions(name, creation_timestamp DESC);

-- ---------------------------------------------------------------------
-- MLflow 3 LoggedModel.  A logged model is a first-class model artifact
-- owned by an experiment, optionally produced by a source run.  Unlike the
-- registry, it has no name/version namespace: ``model_id`` is the identity,
-- and ``models:/<model_id>`` is the load URI.  Params are immutable, tags are
-- mutable, metrics are append-only (latest value wins).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logged_models (
    model_id               TEXT PRIMARY KEY,
    experiment_id          INTEGER NOT NULL,
    name                   TEXT NOT NULL,
    model_type             TEXT NOT NULL DEFAULT '',
    source_run_id          TEXT,
    status                 TEXT NOT NULL DEFAULT 'LOGGED_MODEL_PENDING'
                               CHECK (status IN ('LOGGED_MODEL_PENDING',
                                                 'LOGGED_MODEL_READY',
                                                 'LOGGED_MODEL_UPLOAD_FAILED')),
    status_message         TEXT NOT NULL DEFAULT '',
    artifact_location      TEXT NOT NULL,
    creation_timestamp     INTEGER NOT NULL,
    last_updated_timestamp INTEGER NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    FOREIGN KEY (source_run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_logged_models_experiment_created
    ON logged_models(experiment_id, creation_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_logged_models_source_run
    ON logged_models(source_run_id);
CREATE INDEX IF NOT EXISTS idx_logged_models_name
    ON logged_models(name);

CREATE TABLE IF NOT EXISTS logged_model_tags (
    model_id TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (model_id, key),
    FOREIGN KEY (model_id) REFERENCES logged_models(model_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS logged_model_params (
    model_id TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (model_id, key),
    FOREIGN KEY (model_id) REFERENCES logged_models(model_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS logged_model_metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id  TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     REAL NOT NULL,
    timestamp INTEGER NOT NULL,
    step      INTEGER NOT NULL DEFAULT 0 CHECK (step >= 0),
    FOREIGN KEY (model_id) REFERENCES logged_models(model_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_logged_model_metrics_latest
    ON logged_model_metrics(model_id, key, step DESC, timestamp DESC, id DESC);

CREATE TABLE IF NOT EXISTS traces (
    trace_id         TEXT PRIMARY KEY,
    experiment_id    INTEGER NOT NULL DEFAULT 0,
    name             TEXT NOT NULL,
    timestamp        INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'OK',
    tags_json        TEXT NOT NULL DEFAULT '{}',
    request_metadata_json TEXT NOT NULL DEFAULT '{}',
    request_json     TEXT,
    response_json    TEXT,
    created_by       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_traces_experiment_time
    ON traces(experiment_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);

CREATE TABLE IF NOT EXISTS spans (
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT,
    name           TEXT NOT NULL,
    span_type      TEXT NOT NULL DEFAULT 'UNKNOWN',
    start_time_ns  INTEGER NOT NULL,
    end_time_ns    INTEGER,
    status         TEXT NOT NULL DEFAULT 'OK',
    inputs_json    TEXT,
    outputs_json   TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    events_json    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (trace_id, span_id),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, start_time_ns);

-- ---------------------------------------------------------------------
-- GenAI control plane: feedback/expectations attached to traces.
-- Identity is (trace, name, source, expectation) so a human can correct an
-- earlier judgment without losing the audit trail of updates.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id  TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    name           TEXT NOT NULL,
    value_json     TEXT NOT NULL,
    value_type     TEXT NOT NULL,
    source_type    TEXT NOT NULL DEFAULT 'HUMAN',
    source_id      TEXT NOT NULL DEFAULT '',
    rationale      TEXT NOT NULL DEFAULT '',
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    is_expectation INTEGER NOT NULL DEFAULT 0,
    valid          INTEGER NOT NULL DEFAULT 1,
    create_time    INTEGER NOT NULL,
    last_update_time INTEGER NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_assessments_identity
    ON assessments(trace_id, name, source_type, source_id, is_expectation);
CREATE INDEX IF NOT EXISTS idx_assessments_trace ON assessments(trace_id, name);
CREATE INDEX IF NOT EXISTS idx_assessments_name_value ON assessments(name, value_type, valid);

-- ---------------------------------------------------------------------
-- Prompt registry. Versions are immutable; aliases are mutable pointers.
-- Tags live on versions (the thing you evaluate), not on the prompt name.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompts (
    name                 TEXT PRIMARY KEY,
    creation_timestamp   INTEGER NOT NULL,
    last_updated_timestamp INTEGER NOT NULL,
    description          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    name               TEXT NOT NULL,
    version            INTEGER NOT NULL,
    template           TEXT NOT NULL,
    commit_message     TEXT NOT NULL DEFAULT '',
    creation_timestamp INTEGER NOT NULL,
    tags_json          TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (name, version),
    FOREIGN KEY (name) REFERENCES prompts(name) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS prompt_aliases (
    name    TEXT NOT NULL,
    alias   TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (name, alias),
    FOREIGN KEY (name, version) REFERENCES prompt_versions(name, version)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_name_time
    ON prompt_versions(name, creation_timestamp DESC);

-- ---------------------------------------------------------------------
-- Evaluation datasets.  A dataset is a first-class, versioned asset:
-- metadata lives in ``datasets`` and each sample is an immutable row in
-- ``dataset_records``.  Merging appends rows and updates the metadata in one
-- transaction; the digest covers the complete record set.  Records are JSON
-- objects shaped as {inputs, expectations, tags}.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    version          INTEGER NOT NULL CHECK (version >= 1),
    digest           TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    source_type      TEXT NOT NULL DEFAULT '',
    schema_json      TEXT NOT NULL DEFAULT '{}',
    profile_json     TEXT NOT NULL DEFAULT '{}',
    tags_json        TEXT NOT NULL DEFAULT '{}',
    record_count     INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    created_time     INTEGER NOT NULL,
    last_updated_time INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_datasets_name_created
    ON datasets(name, created_time DESC);
CREATE INDEX IF NOT EXISTS idx_datasets_digest
    ON datasets(digest);

CREATE TABLE IF NOT EXISTS dataset_records (
    dataset_id  TEXT NOT NULL,
    record_id   INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (dataset_id, record_id),
    FOREIGN KEY (dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------
-- Webhooks / outbox. A webhook subscribes to ``entity.action`` events.
-- Deliveries are persisted before the HTTP call so a crash cannot silently
-- drop an event; the dispatcher retries with exponential backoff. This is an
-- at-least-once design: receivers must de-duplicate by delivery_id.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhooks (
    webhook_id           TEXT PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE,
    url                  TEXT NOT NULL,
    events_json          TEXT NOT NULL,
    secret               TEXT NOT NULL DEFAULT '',
    description          TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'ACTIVE'
                             CHECK (status IN ('ACTIVE', 'DISABLED')),
    creation_timestamp   INTEGER NOT NULL,
    last_updated_timestamp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_webhooks_status ON webhooks(status);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id       TEXT PRIMARY KEY,
    webhook_id        TEXT NOT NULL,
    event             TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING'
                          CHECK (status IN ('PENDING', 'DELIVERED', 'FAILED')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT NOT NULL DEFAULT '',
    response_status   INTEGER,
    created_time      INTEGER NOT NULL,
    next_attempt_time INTEGER NOT NULL,
    delivered_time    INTEGER,
    FOREIGN KEY (webhook_id) REFERENCES webhooks(webhook_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
    ON webhook_deliveries(status, next_attempt_time);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook
    ON webhook_deliveries(webhook_id, created_time DESC);

-- ---------------------------------------------------------------------
-- Model monitoring.  MLflow is the metadata/asset plane, not a monitoring
-- system: a monitor stores a derived baseline profile and a feature contract,
-- while each window stores the statistics computed from one batch of
-- production records.  Reports are immutable evidence, not live counters.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitors (
    monitor_id             TEXT PRIMARY KEY,
    name                   TEXT NOT NULL UNIQUE,
    model_name             TEXT NOT NULL DEFAULT '',
    model_version          TEXT NOT NULL DEFAULT '',
    baseline_json          TEXT NOT NULL,
    thresholds_json        TEXT NOT NULL DEFAULT '{}',
    tags_json              TEXT NOT NULL DEFAULT '{}',
    baseline_dataset_id    TEXT NOT NULL DEFAULT '',
    baseline_dataset_digest TEXT NOT NULL DEFAULT '',
    created_time           INTEGER NOT NULL,
    last_updated_time      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitors_model
    ON monitors(model_name, model_version, created_time DESC);

CREATE TABLE IF NOT EXISTS monitor_windows (
    window_id     TEXT PRIMARY KEY,
    monitor_id    TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL,
    sample_count  INTEGER NOT NULL CHECK (sample_count >= 0),
    status        TEXT NOT NULL DEFAULT 'STABLE'
                      CHECK (status IN ('STABLE', 'WARN', 'ALERT')),
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    report_json   TEXT NOT NULL DEFAULT '{}',
    created_time  INTEGER NOT NULL,
    FOREIGN KEY (monitor_id) REFERENCES monitors(monitor_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_monitor_windows_monitor_time
    ON monitor_windows(monitor_id, window_end DESC, created_time DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_windows_status
    ON monitor_windows(monitor_id, status, window_end DESC);
