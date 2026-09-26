import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .domain import DomainError


def canonical(data) -> bytes:
    return json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(data) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()[:24]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, value):
    atomic_write(path, canonical(value))


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, kind: str, ident: str, extension="json") -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", ident):
            raise DomainError("INVALID_ID", "Некорректный идентификатор")
        return self.root / kind / f"{ident}.{extension}"

    @lru_cache(maxsize=32)
    def read(self, kind: str, ident: str):
        try:
            return json.loads(self.path(kind, ident).read_bytes())
        except FileNotFoundError:
            raise DomainError("VERSION_NOT_FOUND", "Запрошенная версия отсутствует", 404) from None

    def put(self, kind: str, ident: str, data):
        path = self.path(kind, ident)
        encoded = canonical(data)
        with self.lock("artifact"):
            if path.exists():
                if path.read_bytes() != encoded:
                    raise DomainError("IMMUTABLE_VERSION_CONFLICT", "Опубликованная версия неизменяема", 409)
                return
            atomic_write(path, encoded)

    @contextmanager
    def lock(self, name="publish"):
        with (self.root / f".{name}.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def current(self):
        try:
            return json.loads((self.root / "current.json").read_bytes())
        except FileNotFoundError:
            return {}

    def publish(self, **changes):
        with self.lock():
            current = self.current()
            current.update(changes)
            current.pop("snapshotId", None)
            current["snapshotId"] = "snapshot-" + digest(current)
            self.put("snapshots", current["snapshotId"], current)
            write_json(self.root / "current.json", current)
            return current
