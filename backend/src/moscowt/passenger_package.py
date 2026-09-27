"""Build and atomically install the compact, offline passenger research release.

Only the builder needs Shapely/PROJ. Serving never imports the research pipeline.
"""

import gzip
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .constants.passenger_package import (
    CATEGORIES as CATEGORIES,
    KEYS as KEYS,
    NOTES as NOTES,
    TABLES as TABLES,
)
from .domain import DomainError
from .storage import SnapshotStore, digest, file_hash, write_json


def invalid(message):
    raise DomainError("PASSENGER_PACKAGE_INVALID", message, 409)


def build_package(source: Path, output: Path):
    import pyarrow.parquet as pq
    from pyproj import Transformer
    from shapely import from_wkb

    source, output = Path(source), Path(output)
    original = json.loads((source / "manifest.json").read_bytes())
    validation = json.loads((source / "validation.json").read_bytes())
    if not validation.get("passed"):
        invalid("Исследовательские проверки не пройдены")
    infra = pd.read_parquet(source / "route_infrastructure_monthly.parquet")
    months = sorted(infra.snapshot.unique())
    names = [n + ".parquet" for n in TABLES]
    names += [f"{prefix}_{m}.parquet" for m in months for prefix in ("poi", "stop_poi_links")]
    names += ["sources.json", "validation.json", "ingestion.json"]
    for name in names:
        expected = original["files"].get(name)
        path = source / name
        if not expected or not path.is_file() or file_hash(path) != expected["sha256"]:
            invalid(f"Исходник не прошёл проверку: {name}")
    total = pd.read_parquet(source / "route_hour_total.parquet")
    cat = pd.read_parquet(source / "route_hour_category.parquet")
    grouped = cat.groupby(KEYS["route_hour_total"]).boardings.sum().sort_index()
    reference = total.set_index(KEYS["route_hour_total"]).boardings.sort_index()
    if not grouped.equals(reference):
        invalid("Категории не сходятся с почасовыми итогами")
    ingestion = json.loads((source / "ingestion.json").read_bytes())
    if int(total.boardings.sum()) != ingestion["successful_in_window"]:
        invalid("Не сходится контрольная сумма валидаций")
    start, end = original["analytical_window"]
    if not pd.to_datetime(total.date).between(pd.Timestamp(start), pd.Timestamp(end), inclusive="left").all():
        invalid("События выходят за аналитическое окно")
    output.parent.mkdir(parents=True, exist_ok=True)
    transformer = Transformer.from_crs("EPSG:32637", "EPSG:4326", always_xy=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".passenger-build-") as tmp:
        staging = Path(tmp)
        entries = {}
        for name in names:
            target = staging / name
            if not name.endswith(".parquet"):
                shutil.copyfile(source / name, target)
                entries[name] = {"bytes": target.stat().st_size, "sha256": file_hash(target)}
                continue
            frame = pd.read_parquet(source / name)
            key = KEYS.get(Path(name).stem)
            if name.startswith("poi_"):
                key = ["object_id"]
                geo = json.loads(pq.read_schema(source / name).metadata[b"geo"])
                if geo["columns"]["geometry"]["crs"]["id"] != {"authority": "EPSG", "code": 32637}:
                    invalid("Неожиданная система координат исходных POI")
                points = [from_wkb(g).representative_point() for g in frame.geometry]
                lon, lat = transformer.transform([p.x for p in points], [p.y for p in points])
                frame = frame.drop(columns="geometry").assign(longitude=lon, latitude=lat)
                if not (frame.longitude.between(36, 39) & frame.latitude.between(54, 57)).all():
                    invalid("Координаты POI вне Москвы")
            if name.startswith("stop_poi_links_"):
                key = ["route", "stop_occurrence_id", "object_id"]
                if not frame.distance_m.between(0, 1000.000001).all():
                    invalid("Некорректное расстояние POI")
            if key and frame.duplicated(key).any():
                invalid(f"Повтор ключа: {name}")
            if "route" in frame:
                frame.route = frame.route.astype(str)
            if "category" in frame and name in ("route_hour_category.parquet", "category_summary.parquet"):
                if not set(frame.category).issubset(CATEGORIES):
                    invalid("Неизвестная категория билета")
            if "boardings" in frame:
                if not (np.isfinite(frame.boardings) & frame.boardings.ge(0)).all():
                    invalid(f"Некорректное число валидаций: {name}")
            if name.startswith("route_hour_"):
                if not frame.hour.between(0, 23).all():
                    invalid("Некорректный час")
                frame["timestamp"] = (
                    pd.to_datetime(frame.date) + pd.to_timedelta(frame.hour, unit="h")
                ).dt.tz_localize("Europe/Moscow")
            frame.to_parquet(target, index=False, compression="zstd")
            entries[name] = {"bytes": target.stat().st_size, "sha256": file_hash(target), "rows": len(frame)}
        manifest = {
            "schema": 1,
            "source_manifest_sha256": file_hash(source / "manifest.json"),
            "coverage": {"start": start + "T00:00:00+03:00", "end": end + "T00:00:00+03:00"},
            "timezone": "Europe/Moscow",
            "taxonomy_version": "1.0",
            "categories": CATEGORIES,
            "routes": sorted(infra.route.astype(str).unique()),
            "snapshots": months,
            "total_boardings": int(total.boardings.sum()),
            "notes": NOTES,
            "unique_cards": ingestion["successful_unique_cards_in_window"],
            "source_checks": len(validation["checks"]),
            "files": entries,
            "attribution": "© OpenStreetMap contributors (ODbL 1.0); © WorldPop (CC BY 4.0)",
            "poi_geometry": "representative point in WGS84; distances preserved from source geometry",
        }
        manifest["id"] = "passengers-" + digest(manifest)
        write_json(staging / "manifest.json", manifest)
        pending = staging / "package.tar.gz"
        with pending.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for name in sorted([*entries, "manifest.json"]):
                    info = tarfile.TarInfo(name)
                    info.size = (staging / name).stat().st_size
                    info.mode = 0o644
                    with (staging / name).open("rb") as stream:
                        archive.addfile(info, stream)
        os.replace(pending, output)
    return {"id": manifest["id"], "bytes": output.stat().st_size, "sha256": file_hash(output)}


def read_manifest(folder):
    try:
        value = json.loads((Path(folder) / "manifest.json").read_bytes())
        ident = value["id"]
        if value["schema"] != 1 or ident != "passengers-" + digest(
            {k: v for k, v in value.items() if k != "id"}
        ):
            invalid("Некорректная версия пассажирского пакета")
        for name in value["files"]:
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+\.(parquet|json)", name) or ".." in name:
                invalid("Некорректный путь пассажирского пакета")
        if not set(n + ".parquet" for n in TABLES).issubset(value["files"]):
            invalid("Пассажирский пакет неполон")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DomainError(
            "PASSENGER_PACKAGE_INVALID", "Пассажирский пакет отсутствует или повреждён", 503
        ) from exc


def verify_package(folder):
    manifest = read_manifest(folder)
    for name, item in manifest["files"].items():
        path = Path(folder) / name
        if not path.is_file() or path.stat().st_size != item["bytes"] or file_hash(path) != item["sha256"]:
            invalid(f"Повреждён файл пассажирского пакета: {name}")
    return manifest


def activate_package(root: Path, ident: str):
    if not re.fullmatch(r"passengers-[0-9a-f]{24}", ident):
        invalid("Некорректный идентификатор пакета")
    repo = SnapshotStore(Path(root) / "passengers")
    with repo.lock("install"):
        verify_package(repo.root / ident)
        write_json(repo.root / "current.json", {"id": ident})
    return {"id": ident}


def install_package(root: Path, archive_path: Path, *, bundled=False):
    repo = SnapshotStore(Path(root) / "passengers")
    with repo.lock("install"), tempfile.TemporaryDirectory(dir=repo.root, prefix=".install-") as tmp:
        staging = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > 200 or sum(m.size for m in members) > 512 * 1024**2:
                invalid("Слишком большой пассажирский пакет")
            if len({m.name for m in members}) != len(members):
                invalid("Повтор пути в пассажирском пакете")
            for member in members:
                if not member.isfile() or not re.fullmatch(r"[a-zA-Z0-9_.-]+\.(json|parquet)", member.name):
                    invalid("Недопустимый элемент пассажирского пакета")
                with archive.extractfile(member) as src, (staging / member.name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
        manifest = verify_package(staging)
        if {m.name for m in members} != {*manifest["files"], "manifest.json"}:
            invalid("Лишние файлы в пассажирском пакете")
        ident = manifest["id"]
        destination = repo.root / ident
        if destination.exists():
            verify_package(destination)
        else:
            os.rename(staging, destination)
        release = repo.root / "release.json"
        # Remember the delivered release so an explicit rollback survives restart.
        previous_release = json.loads(release.read_bytes()).get("id") if release.exists() else None
        if not bundled or previous_release != ident or not (repo.root / "current.json").exists():
            write_json(repo.root / "current.json", {"id": ident})
        if bundled:
            write_json(release, {"id": ident})
    return {"id": ident}
