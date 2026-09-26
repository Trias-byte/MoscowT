import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .domain import DomainError
from .storage import canonical, digest


class JobRepository:
    def __init__(self, root: Path, limit=32):
        self.root, self.limit = root, limit
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript((Path(__file__).parent / "migrations/001_jobs.sql").read_text())

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "service.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, kind, payload, key):
        if not key or len(key) > 128:
            raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Нужен Idempotency-Key длиной 1–128 символов")
        hashed = digest(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM jobs WHERE kind=? AND idempotency_key=?", (kind, key)
            ).fetchone()
            if existing:
                if existing["request_hash"] != hashed:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT", "Этот ключ уже использован с другим запросом", 409
                    )
                return self.public(existing)
            pending = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('pending','running')"
            ).fetchone()[0]
            if pending >= self.limit:
                raise DomainError("QUEUE_FULL", "Очередь заполнена; повторите позже", 429)
            ident, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO jobs(id,kind,idempotency_key,request_hash,payload,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'pending',?,?)",
                (ident, kind, key, hashed, canonical(payload).decode(), now, now),
            )
            return {"id": ident, "status": "pending", "kind": kind, "downloadUrl": None, "error": None}

    def public(self, row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "error": row["error"],
            "downloadUrl": f"/api/v1/exports/{row['id']}/download" if row["status"] == "ready" else None,
        }

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise DomainError("JOB_NOT_FOUND", "Задание не найдено", 404)
        return dict(row)

    def recover(self):
        # Only called by the worker that holds the exclusive process lock.
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END, "
                "error=CASE WHEN attempts>=3 THEN 'Worker repeatedly interrupted' ELSE NULL END "
                "WHERE status='running'"
            )

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def finish(self, ident, filename=None, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?, filename=?, error=?, updated_at=? WHERE id=?",
                (
                    "failed" if error else "ready",
                    filename,
                    error,
                    datetime.now(timezone.utc).isoformat(),
                    ident,
                ),
            )
