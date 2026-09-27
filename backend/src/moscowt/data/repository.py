"""Shared immutable datasets with preview/apply ingestion and monthly partitions."""

import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..domain import DomainError
from ..storage import SnapshotStore, atomic_write, digest, file_hash, write_json
from .catalog import DataCatalog
from .normalization import KEYS, SourceNormalizer
from .schemas import ImportSpec


class DatasetRepository:
    SCHEMA_VERSION = 1

    def __init__(self, root: Path, normalizer: SourceNormalizer | None = None, read_only=False):
        self.root = Path(root).resolve()
        self.files = SnapshotStore(self.root / "canonical")
        self.catalog = DataCatalog(self.root / "catalog.sqlite3", read_only=read_only)
        self.normalizer = normalizer or SourceNormalizer()

    def _upload_dir(self, ident):
        if not re.fullmatch(r"upload-[0-9a-f]{32}", ident):
            raise DomainError("INVALID_UPLOAD_ID", "Некорректный идентификатор загрузки")
        return self.root / "incoming" / ident

    def manifest(self, ident):
        return self.files.read("datasets", ident)

    def frame(self, ident, *, route_ids=None, start=None, end=None):
        manifest = self.manifest(ident)
        frames = []
        for part in manifest["partitions"]:
            if start is not None and part["month"] < pd.Timestamp(start).strftime("%Y-%m"):
                continue
            if end is not None and part["month"] > pd.Timestamp(end).strftime("%Y-%m"):
                continue
            filters = []
            if route_ids:
                filters.append(("route", "in", route_ids))
            if start is not None:
                filters.append(
                    ("timestamp", ">=", pd.Timestamp(start).tz_convert("Europe/Moscow").as_unit("ns"))
                )
            if end is not None:
                filters.append(
                    ("timestamp", "<", pd.Timestamp(end).tz_convert("Europe/Moscow").as_unit("ns"))
                )
            frames.append(
                pd.read_parquet(self.files.path("partitions", part["id"], "parquet"), filters=filters or None)
            )
        if not frames:
            return pd.DataFrame(columns=manifest["columns"])
        return pd.concat(frames, ignore_index=True).sort_values(KEYS).reset_index(drop=True)

    def _source(self, source: Path, folder: Path, index: int):
        pending = folder / f"source-{index}.pending"
        checksum = hashlib.sha256()
        with Path(source).open("rb") as src, pending.open("wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                checksum.update(chunk)
                dst.write(chunk)
        sha = checksum.hexdigest()
        destination = self.root / "raw" / sha / "source.csv"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if file_hash(destination) != sha:
                raise DomainError("SOURCE_CORRUPTED", "Контрольная сумма исходника не совпала", 409)
            pending.unlink()
        else:
            # The source is durable before any catalog publication can refer to it.
            import os

            with pending.open("rb") as stream:
                os.fsync(stream.fileno())
            pending.replace(destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {"sha256": sha, "name": Path(source).name, "bytes": destination.stat().st_size}, destination

    def _route_lifetimes(self, frame):
        keep = pd.Series(True, index=frame.index)
        dates = frame.timestamp.dt.strftime("%Y-%m-%d")
        for route in self.catalog.routes():
            belongs = frame.route.eq(route["id"])
            outside = pd.Series(False, index=frame.index)
            if route["valid_from"]:
                outside |= dates < route["valid_from"]
            if route["valid_to"]:
                outside |= dates >= route["valid_to"]
            outside &= belongs
            provided = frame.value_origin.isin(["label", "events"])
            if (outside & provided).any():
                raise DomainError("OUTSIDE_ROUTE_LIFETIME", "Данные выходят за период работы маршрута")
            keep &= ~outside
        return frame.loc[keep].copy()

    @staticmethod
    def _merge(previous, incoming, spec):
        if previous.empty:
            return incoming.copy(), {"overlap_keys": 0, "conflict_keys": 0, "changed_keys": len(incoming)}
        old, new = previous.set_index(KEYS), incoming.set_index(KEYS)
        overlap = old.index.intersection(new.index)
        left, right = old.loc[overlap, "value"], new.loc[overlap, "value"]
        different = ~(left.eq(right) | (left.isna() & right.isna()))
        conflicts = int((different & left.notna() & right.notna()).sum())
        stats = {
            "overlap_keys": len(overlap),
            "conflict_keys": conflicts,
            "changed_keys": int(different.sum()) + len(new.index.difference(old.index)),
        }
        if spec.mode == "replace":
            selected = previous.route.isin(spec.route_ids)
            selected &= previous.timestamp.ge(spec.time_range.start) & previous.timestamp.lt(
                spec.time_range.end
            )
            old = previous.loc[~selected].set_index(KEYS)
            result = pd.concat([old, new])
        else:
            # A partial import cannot erase a previously known value with a missing row.
            replace = new.index.difference(old.index)
            same = old.index.intersection(new.index)
            if spec.mode == "upsert":
                replace = replace.union(same[new.loc[same, "value"].notna()])
            else:
                replace = replace.union(same[old.loc[same, "value"].isna() & new.loc[same, "value"].notna()])
            result = pd.concat([old.drop(index=replace, errors="ignore"), new.loc[replace]])
        return result.reset_index().sort_values(KEYS).reset_index(drop=True), stats

    def preview(self, sources: list[Path], spec: ImportSpec):
        if not 1 <= len(sources) <= 100:
            raise DomainError("INVALID_SOURCE_COUNT", "Нужно от 1 до 100 файлов")
        ident = "upload-" + uuid.uuid4().hex
        folder = self._upload_dir(ident)
        folder.mkdir(parents=True)
        base = None if spec.new_dataset else spec.base_dataset_id or self.catalog.current_id()
        if base:
            self.manifest(base)
        source_meta, paths = {}, []
        for index, source in enumerate(sources):
            meta, path = self._source(source, folder, index)
            if meta["sha256"] not in source_meta:
                paths.append(path)
                source_meta[meta["sha256"]] = meta
        definition = {
            "schema": self.SCHEMA_VERSION,
            "sources": sorted(source_meta),
            "spec": spec.model_dump(mode="json"),
        }
        # Explicit branches are independent of the mutable current pointer.
        if spec.base_dataset_id or spec.new_dataset:
            definition["base_dataset_id"] = base
        fingerprint = digest(definition)
        report = {
            "id": ident,
            "fingerprint": fingerprint,
            "spec": definition["spec"],
            "base_dataset_id": base,
            "sources": list(source_meta.values()),
            "duplicate": False,
            "route_versions": {r["id"]: r["version"] for r in self.catalog.routes()},
        }
        existing = self.catalog.imported(fingerprint)
        if existing:
            report.update(
                duplicate=True,
                dataset_id=existing["dataset_id"],
                rows=existing["rows"],
                routes=existing["routes"],
            )
        else:
            parsed = self.normalizer.read(paths, spec, folder)
            incoming = self._route_lifetimes(parsed.frame)
            if incoming.empty:
                raise DomainError("EMPTY_SELECTION", "Нет данных в периоде работы маршрутов")
            incoming["source_import"] = fingerprint
            incoming.to_parquet(folder / "normalized.parquet", index=False)
            previous = self.frame(base) if base else pd.DataFrame(columns=incoming.columns)
            _, stats = self._merge(previous, incoming, spec)
            report.update(
                rows=len(incoming),
                input_rows=parsed.input_rows,
                selected_rows=parsed.selected_rows,
                routes=sorted(incoming.route.unique().tolist()),
                missing_hours=int(incoming.value.isna().sum()),
                total=float(incoming.value.sum()),
                event_files=parsed.event_files,
                **stats,
            )
        write_json(folder / "preview.json", report)
        return report

    def get_preview(self, ident):
        try:
            return json.loads((self._upload_dir(ident) / "preview.json").read_bytes())
        except FileNotFoundError:
            raise DomainError("UPLOAD_NOT_FOUND", "Загрузка не найдена", 404) from None

    def apply(self, ident):
        report = self.get_preview(ident)
        old = self.catalog.imported(report["fingerprint"])
        if old:
            return {**old, "duplicate": True, "current_dataset_id": self.catalog.current_id()}
        base = report["base_dataset_id"]
        spec = ImportSpec.model_validate(report["spec"])
        activate = not (spec.new_dataset or spec.base_dataset_id)
        if activate and self.catalog.current_id() != base:
            raise DomainError("DATASET_CHANGED", "Данные изменились; проверьте загрузку повторно", 409)
        versions = {r["id"]: r["version"] for r in self.catalog.routes()}
        for route_id in report["routes"]:
            if versions.get(route_id) != report["route_versions"].get(route_id):
                raise DomainError("ROUTE_CHANGED", "Маршрут изменился; проверьте загрузку повторно", 409)
        if spec.mode == "append" and report["conflict_keys"]:
            raise DomainError("DATA_CONFLICT", "Есть отличающиеся значения; выберите режим исправления", 409)
        folder = self._upload_dir(ident)
        incoming = pd.read_parquet(folder / "normalized.parquet")
        previous = self.frame(base) if base else pd.DataFrame(columns=incoming.columns)
        frame, _ = self._merge(previous, incoming, spec)
        parts = []
        for month, rows in frame.groupby(frame.timestamp.dt.strftime("%Y-%m"), sort=True):
            buffer = io.BytesIO()
            rows.reset_index(drop=True).to_parquet(buffer, index=False)
            content = buffer.getvalue()
            sha = hashlib.sha256(content).hexdigest()
            part_id = "part-" + sha[:24]
            path = self.files.path("partitions", part_id, "parquet")
            if not path.exists():
                atomic_write(path, content)
            elif file_hash(path) != sha:
                raise DomainError("DATASET_CORRUPTED", "Повреждён раздел датасета", 409)
            parts.append({"month": month, "id": part_id, "sha256": sha, "rows": len(rows)})
        sources = dict(self.manifest(base)["imports"]) if base else {}
        events = []
        for filename in report["event_files"]:
            source = folder / filename
            event_id = "events-" + file_hash(source)[:24]
            target = self.files.path("events", event_id, "parquet")
            if not target.exists():
                atomic_write(target, source.read_bytes())
            events.append({"id": event_id, "sha256": file_hash(source)})
        sources[report["fingerprint"]] = {
            "sources": report["sources"],
            "spec": report["spec"],
            "events": events,
        }
        active = set(frame.source_import)
        sources = {key: value for key, value in sources.items() if key in active}
        dataset_id = "dataset-" + digest({"base": base, "import": report["fingerprint"], "parts": parts})
        routes = sorted(frame.route.unique().tolist())
        manifest = {
            "id": dataset_id,
            "name": spec.dataset_name
            or (self.manifest(base).get("name") if base else None)
            or "История валидаций",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "collection_id": self.manifest(base).get("collection_id", base) if base else dataset_id,
            "revision": self.manifest(base).get("revision", 1) + 1 if base else 1,
            "schema_version": self.SCHEMA_VERSION,
            "parent_id": base,
            "timezone": "Europe/Moscow",
            "metric": "successful_validations",
            "start": frame.timestamp.min().isoformat(),
            "end": (frame.timestamp.max() + pd.Timedelta(hours=1)).isoformat(),
            "rows": len(frame),
            "known_rows": int(frame.value.notna().sum()),
            "total": float(frame.value.sum()),
            "routes": routes,
            "columns": list(frame.columns),
            "partitions": parts,
            "imports": sources,
        }
        self.files.put("datasets", dataset_id, manifest)
        result = {
            "dataset_id": dataset_id,
            "upload_id": ident,
            "rows": len(frame),
            "routes": routes,
            "duplicate": False,
            "current_dataset_id": dataset_id if activate else self.catalog.current_id(),
        }
        return self.catalog.publish(manifest, report["fingerprint"], result, base, activate=activate)

    def event_batches(self, ident, *, route_ids=None, start=None, end=None):
        """Only yield events still authoritative for the dataset's route-hour keys."""
        frame = self.frame(ident, route_ids=route_ids, start=start, end=end)
        for fingerprint, source in self.manifest(ident)["imports"].items():
            keys = frame.loc[frame.source_import.eq(fingerprint), KEYS]
            if keys.empty:
                continue
            for part in source["events"]:
                path = self.files.path("events", part["id"], "parquet")
                batch = pd.read_parquet(path).merge(keys, on=KEYS, how="inner", validate="many_to_one")
                if not batch.empty:
                    yield batch

    def verify(self, ident):
        manifest = self.manifest(ident)
        for part in manifest["partitions"]:
            if file_hash(self.files.path("partitions", part["id"], "parquet")) != part["sha256"]:
                raise DomainError("DATASET_CORRUPTED", "Контрольная сумма раздела не совпала", 409)
        frame = self.frame(ident)
        if frame.duplicated(KEYS).any() or len(frame) != manifest["rows"]:
            raise DomainError("DATASET_CORRUPTED", "Нарушены ключи датасета", 409)
        return {"dataset_id": ident, "rows": len(frame), "verified": True}
