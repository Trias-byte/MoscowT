"""Portable checksummed artifacts; runtime SQLite files and pending jobs are excluded."""

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .data.catalog import DataCatalog
from .data.repository import DatasetRepository
from .domain import DomainError
from .storage import canonical, file_hash

STATE_KINDS = (
    "histories",
    "networks",
    "route_imports",
    "models",
    "trained_models",
    "forecasts",
    "forecast_runs",
    "fleets",
    "snapshots",
    "scenarios",
    "evaluations",
    "reports",
)
DATA_KINDS = ("canonical", "raw", "schedules", "schedule_sources", "external", "weather_forecasts")


def create_bundle(settings, ident):
    # Capture mutable pointers before listing their immutable dependencies. A user
    # may publish another forecast while the worker compresses this archive.
    captured = {}
    for name in ("current.json", "demo.json", "backtest.json", "submission.csv"):
        path = settings.state_dir / name
        if path.is_file():
            captured["state/" + name] = path.read_bytes()
    data = DatasetRepository(settings.data_root)
    catalog = data.catalog.export_snapshot()
    for dataset in catalog["datasets"]:
        data.verify(dataset["id"])
    sources = []
    for root, prefix, directories in (
        (settings.state_dir, "state", STATE_KINDS),
        (settings.data_root, "data", DATA_KINDS),
    ):
        for directory in directories:
            sources.extend(
                (path, f"{prefix}/{path.relative_to(root)}")
                for path in (root / directory).rglob("*")
                if path.is_file() and not path.name.startswith(".")
            )
    entries = [
        {"path": name, "sha256": file_hash(path), "bytes": path.stat().st_size}
        for path, name in sorted(sources, key=lambda pair: pair[1])
    ]
    entries.extend(
        {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        for name, content in captured.items()
    )
    manifest = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": entries,
        "catalog": catalog,
    }
    directory = settings.state_dir / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{ident}.tar.gz"
    fd, pending = tempfile.mkstemp(dir=directory, prefix=".pending-")
    os.close(fd)
    try:
        with tarfile.open(pending, "w:gz") as archive:
            for path, name in sources:
                archive.add(path, arcname=name, recursive=False)
            for name, content in captured.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            content = canonical(manifest)
            info = tarfile.TarInfo("manifest.json")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        os.replace(pending, target)
    finally:
        Path(pending).unlink(missing_ok=True)
    return {
        "filename": target.name,
        "sha256": file_hash(target),
        "files": len(entries),
        "download_url": f"/api/v2/jobs/{ident}/download",
    }


def restore_bundle(path, settings):
    # Restore only into empty directories; a failed restore cannot damage an active service.
    for root in (settings.state_dir, settings.data_root):
        if root.exists() and any(root.iterdir()):
            raise DomainError("RESTORE_TARGET_NOT_EMPTY", "Восстановление требует пустых каталогов")
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        with tarfile.open(path, "r:gz") as archive:
            manifest = json.load(archive.extractfile("manifest.json"))
            expected = {item["path"]: item for item in manifest["files"]}
            if len(expected) != len(manifest["files"]):
                raise DomainError("BUNDLE_INVALID", "Повтор пути в manifest")
            for name, item in expected.items():
                relative = Path(name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.parts[0] not in ("state", "data")
                ):
                    raise DomainError("BUNDLE_INVALID", "Недопустимый путь архива")
                member = archive.getmember(name)
                if not member.isfile() or member.size != item["bytes"]:
                    raise DomainError("BUNDLE_INVALID", "Некорректный элемент архива")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if file_hash(target) != item["sha256"]:
                    raise DomainError("BUNDLE_CORRUPT", "Контрольная сумма архива не совпадает")
        for kind, root in (("state", settings.state_dir), ("data", settings.data_root)):
            shutil.copytree(staging / kind, root, dirs_exist_ok=True)
    catalog = DataCatalog(settings.data_root / "catalog.sqlite3")
    with catalog.connect() as db:
        for route in manifest["catalog"].get("route_versions", []):
            catalog._save_route(db, route)
        for route in manifest["catalog"]["routes"]:
            catalog._save_route(db, route)
        for dataset in manifest["catalog"]["datasets"]:
            db.execute(
                "INSERT INTO datasets(id,manifest,created_at) VALUES (?,?,?)",
                (dataset["id"], canonical(dataset).decode(), manifest["created_at"]),
            )
            for fingerprint in dataset["imports"]:
                result = {
                    "dataset_id": dataset["id"],
                    "rows": dataset["rows"],
                    "routes": dataset["routes"],
                    "current_dataset_id": dataset["id"],
                }
                db.execute(
                    "INSERT OR IGNORE INTO imports VALUES (?,?,?,?)",
                    (fingerprint, dataset["id"], canonical(result).decode(), manifest["created_at"]),
                )
        if manifest["catalog"]["current_id"]:
            db.execute("INSERT INTO catalog_state VALUES ('dataset',?)", (manifest["catalog"]["current_id"],))
    return {"restored_files": len(expected), "dataset_id": catalog.current_id()}
