"""Transactional metadata; numeric data remains in immutable Parquet files."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field, model_validator

from ..domain import DomainError, StrictModel
from ..storage import canonical, digest


class RouteDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    number: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    valid_from: str | None = None
    valid_to: str | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        from datetime import date

        for value in (self.valid_from, self.valid_to):
            if value is not None and date.fromisoformat(value).isoformat() != value:
                raise ValueError("Ожидается дата YYYY-MM-DD")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("Дата окончания должна быть позже начала")
        return self

    def versioned(self):
        value = self.model_dump()
        return {**value, "version": "route-" + digest(value)}


class DataCatalog:
    """Only this repository publishes datasets and changes route definitions."""

    def __init__(self, path: Path, read_only=False):
        self.read_only = read_only
        self.path = Path(path)
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, manifest TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports (
                    fingerprint TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
                    report TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS routes (
                    id TEXT PRIMARY KEY, definition TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS route_versions (
                    route_id TEXT NOT NULL, version TEXT NOT NULL, definition TEXT NOT NULL,
                    PRIMARY KEY(route_id, version)
                );
            """)

    @contextmanager
    def connect(self):
        db = (
            sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            if self.read_only
            else sqlite3.connect(self.path, timeout=30)
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def current_id(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM catalog_state WHERE key='dataset'").fetchone()
        return row[0] if row else None

    def datasets(self):
        with self.connect() as db:
            rows = db.execute("SELECT manifest FROM datasets ORDER BY created_at DESC, id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, ident: str):
        with self.connect() as db:
            row = db.execute("SELECT manifest FROM datasets WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise DomainError("DATASET_NOT_FOUND", "Версия датасета не найдена", 404)
        return json.loads(row[0])

    def routes(self):
        with self.connect() as db:
            rows = db.execute("SELECT definition FROM routes ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def route_versions(self):
        with self.connect() as db:
            rows = db.execute("SELECT definition FROM route_versions ORDER BY route_id, version").fetchall()
        return [json.loads(row[0]) for row in rows]

    def export_snapshot(self):
        """Read catalog metadata at one SQLite snapshot while imports may continue."""
        with self.connect() as db:
            db.execute("BEGIN")
            current = db.execute("SELECT value FROM catalog_state WHERE key='dataset'").fetchone()
            return {
                "datasets": [
                    json.loads(r[0])
                    for r in db.execute("SELECT manifest FROM datasets ORDER BY created_at, id")
                ],
                "routes": [json.loads(r[0]) for r in db.execute("SELECT definition FROM routes ORDER BY id")],
                "route_versions": [
                    json.loads(r[0])
                    for r in db.execute("SELECT definition FROM route_versions ORDER BY route_id, version")
                ],
                "current_id": current[0] if current else None,
            }

    @staticmethod
    def _save_route(db, value):
        encoded = canonical(value).decode()
        db.execute(
            "INSERT INTO routes(id,definition) VALUES (?,?) "
            "ON CONFLICT(id) DO UPDATE SET definition=excluded.definition",
            (value["id"], encoded),
        )
        db.execute(
            "INSERT OR IGNORE INTO route_versions(route_id,version,definition) VALUES (?,?,?)",
            (value["id"], value["version"], encoded),
        )

    def save_route(self, route: RouteDefinition, expected_version: str | None = None):
        value = route.versioned()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT definition FROM routes WHERE id=?", (route.id,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous == value:
                return value
            if previous is not None and previous["version"] != expected_version:
                raise DomainError("ROUTE_CHANGED", "Маршрут уже существует или изменился", 409)
            if previous is None and expected_version is not None:
                raise DomainError("ROUTE_NOT_FOUND", "Маршрут не найден", 404)
            self._save_route(db, value)
        return value

    def imported(self, fingerprint):
        with self.connect() as db:
            row = db.execute("SELECT report FROM imports WHERE fingerprint=?", (fingerprint,)).fetchone()
        return json.loads(row[0]) if row else None

    def publish(self, manifest, fingerprint, report, expected_id):
        """Commit last, after files are durable. Reject concurrent stale previews."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_import = db.execute(
                "SELECT report FROM imports WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if old_import:
                return {**json.loads(old_import[0]), "duplicate": True}
            current = db.execute("SELECT value FROM catalog_state WHERE key='dataset'").fetchone()
            if (current[0] if current else None) != expected_id:
                raise DomainError("DATASET_CHANGED", "Данные изменились; проверьте загрузку повторно", 409)
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT OR IGNORE INTO datasets(id,manifest,created_at) VALUES (?,?,?)",
                (manifest["id"], canonical(manifest).decode(), now),
            )
            for route_id in manifest["routes"]:
                exists = db.execute("SELECT 1 FROM routes WHERE id=?", (route_id,)).fetchone()
                if not exists:
                    route = RouteDefinition(id=route_id, number=route_id, name=f"Маршрут № {route_id}")
                    self._save_route(db, route.versioned())
            db.execute(
                "INSERT INTO imports(fingerprint,dataset_id,report,created_at) VALUES (?,?,?,?)",
                (fingerprint, manifest["id"], canonical(report).decode(), now),
            )
            db.execute(
                "INSERT INTO catalog_state(key,value) VALUES ('dataset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (manifest["id"],),
            )
        return report
