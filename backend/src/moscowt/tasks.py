"""Persistent work queue with leases, ownership and cancellation for platform operations."""

import json
import time
import uuid

from .domain import DomainError
from .jobs import JobRepository
from .storage import canonical, digest

MODEL_KINDS = ("train", "forecast", "scenario_run", "model_import", "model_reweight", "model_export")
GENERAL_KINDS = (
    "import_preview",
    "import_apply",
    "schedule_import",
    "period_export",
    "bundle_export",
    "weather_fetch",
    "factor_fetch",
)


class WorkQueue:
    def __init__(self, root, limit=32):
        self.root, self.limit = root, limit
        self.database = JobRepository(root, limit)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, progress REAL NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'queued', result TEXT, error TEXT,
                    UNIQUE(kind,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS work_pending ON work_items(status, created_at);
            """)

    def connect(self):
        return self.database.connect()

    def enqueue(self, kind, payload, key):
        if kind not in MODEL_KINDS + GENERAL_KINDS:
            raise DomainError("INVALID_JOB_KIND", "Неизвестный тип задания")
        if not key or len(key) > 128:
            raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Нужен Idempotency-Key длиной 1–128 символов")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM work_items WHERE kind=? AND idempotency_key=?", (kind, key)
            ).fetchone()
            if old:
                if old["request_hash"] != digest(payload):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Ключ уже использован для другого запроса", 409)
                return self.public(old)
            count = db.execute(
                "SELECT count(*) FROM work_items WHERE status IN ('pending','running')"
            ).fetchone()[0]
            if count >= self.limit:
                raise DomainError("QUEUE_FULL", "Очередь заполнена", 429)
            ident, now = uuid.uuid4().hex, time.time()
            db.execute(
                "INSERT INTO work_items(id,kind,idempotency_key,request_hash,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                (ident, kind, key, digest(payload), canonical(payload).decode(), now, now),
            )
            return self.public(db.execute("SELECT * FROM work_items WHERE id=?", (ident,)).fetchone())

    def public(self, row):
        return {
            key: (json.loads(row[key]) if row[key] and row["status"] == "ready" else None)
            if key == "result" else row[key]
            for key in (
                "id",
                "kind",
                "status",
                "created_at",
                "updated_at",
                "attempts",
                "cancel_requested",
                "progress",
                "phase",
                "result",
                "error",
            )
        }

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM work_items WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise DomainError("JOB_NOT_FOUND", "Задание не найдено", 404)
        return dict(row)

    def list(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM work_items ORDER BY created_at DESC LIMIT 100").fetchall()
        return [self.public(row) for row in rows]

    def claim(self, kinds, owner):
        now = time.time()
        placeholders = ",".join("?" for _ in kinds)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"UPDATE work_items SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' WHEN attempts>=3 THEN 'failed' ELSE 'pending' END, error='Worker lease expired', owner=NULL WHERE status='running' AND lease_until<? AND kind IN ({placeholders})",
                (now, *kinds),
            )
            row = db.execute(
                f"SELECT * FROM work_items WHERE status='pending' AND kind IN ({placeholders}) ORDER BY created_at LIMIT 1",
                kinds,
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE work_items SET status='running',owner=?,lease_until=?,updated_at=?,attempts=attempts+1,phase='running',error=NULL WHERE id=?",
                (owner, now + 30, now, row["id"]),
            )
            return {**dict(row), "owner": owner, "payload": json.loads(row["payload"])}

    def heartbeat(self, ident, owner):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE work_items SET lease_until=?,updated_at=? WHERE id=? AND owner=? AND status='running' AND cancel_requested=0",
                (time.time() + 30, time.time(), ident, owner),
            ).rowcount
        return bool(changed)

    def finish(self, ident, owner, result=None, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE work_items SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' WHEN ? IS NOT NULL THEN 'failed' ELSE 'ready' END,progress=CASE WHEN ? IS NULL THEN 1 ELSE progress END,phase='finished',result=?,error=?,updated_at=?,lease_until=NULL WHERE id=? AND owner=? AND status='running'",
                (
                    error,
                    error,
                    canonical(result).decode() if result is not None else None,
                    error,
                    time.time(),
                    ident,
                    owner,
                ),
            )

    def cancel(self, ident):
        self.get(ident)
        with self.connect() as db:
            db.execute(
                "UPDATE work_items SET cancel_requested=1,status=CASE WHEN status='pending' THEN 'cancelled' ELSE status END,updated_at=? WHERE id=? AND status IN ('pending','running')",
                (time.time(), ident),
            )
        return self.public(self.get(ident))
