CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','ready','failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    filename TEXT,
    UNIQUE(kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status, created_at);
PRAGMA user_version = 1;
