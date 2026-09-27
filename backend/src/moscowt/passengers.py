"""Read-only passenger aggregates, pinned to an immutable release per request."""

import json
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd

from .domain import DomainError
from .network_history import resolve_network
from .passenger_package import CATEGORIES, verify_package


def records(frame):
    frame = frame.copy()
    for column in frame.select_dtypes(include=["datetimetz"]):
        frame[column] = (
            frame[column]
            .dt.tz_convert("Europe/Moscow")
            .map(lambda value: value.isoformat() if pd.notna(value) else None)
        )
    return json.loads(frame.to_json(orient="records", date_format="iso"))


class PassengerRelease:
    def __init__(self, folder):
        self.folder = folder
        self.manifest = verify_package(folder)
        self.cache, self.bytes, self.lock = OrderedDict(), 0, threading.RLock()

    def frame(self, name, filters=None, columns=None):
        filename = name + ".parquet"
        if filename not in self.manifest["files"]:
            raise DomainError("PASSENGER_TABLE_MISSING", "Таблица отсутствует в пакете", 503)
        key = json.dumps([name, filters, columns], sort_keys=True, default=str)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key][0].copy()
            frame = pd.read_parquet(self.folder / filename, filters=filters, columns=columns)
            size = int(frame.memory_usage(deep=True).sum())
            if size <= 24 * 1024**2:
                while self.cache and (len(self.cache) >= 16 or self.bytes + size > 24 * 1024**2):
                    _, (_, old_size) = self.cache.popitem(last=False)
                    self.bytes -= old_size
                self.cache[key] = (frame, size)
                self.bytes += size
            return frame.copy()

    def meta(self, filters=None, status="ready", reason=None):
        return {
            "dataset_id": self.manifest["id"],
            "coverage": self.manifest["coverage"],
            "timezone": "Europe/Moscow",
            "filters": filters or {},
            "status": status,
            "reason": reason,
        }


class PassengerRepository:
    def __init__(self, root, store, data):
        self.root, self.store, self.data = Path(root) / "passengers", store, data
        self.lock, self.loaded = threading.RLock(), OrderedDict()

    def current(self):
        try:
            pointer = json.loads((self.root / "current.json").read_bytes())
            import re

            ident = pointer["id"]
            if not re.fullmatch(r"passengers-[0-9a-f]{24}", ident):
                raise ValueError("invalid passenger id")
            folder = self.root / ident
            # Detect modified/missing files even if a frame was already cached.
            signature = tuple(
                (p.name, p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size)
                for p in sorted(folder.iterdir())
                if p.is_file()
            )
            with self.lock:
                cached = self.loaded.get(ident)
                if cached and cached[0] == signature:
                    self.loaded.move_to_end(ident)
                    return cached[1]
                release = PassengerRelease(folder)
                self.loaded[ident] = (signature, release)
                while len(self.loaded) > 2:
                    self.loaded.popitem(last=False)
                return release
        except FileNotFoundError as exc:
            raise DomainError("PASSENGERS_UNAVAILABLE", "Пассажирский пакет не установлен", 503) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("PASSENGER_PACKAGE_INVALID", "Повреждён пассажирский пакет", 503) from exc

    def metadata(self):
        try:
            r = self.current()
        except DomainError as error:
            return {
                "available": False,
                "status": "missing" if error.code == "PASSENGERS_UNAVAILABLE" else "corrupt",
                "reason": error.message,
            }
        m = r.manifest
        return {
            "available": True,
            "status": "ready",
            **{
                k: m[k]
                for k in (
                    "id",
                    "coverage",
                    "timezone",
                    "taxonomy_version",
                    "categories",
                    "routes",
                    "snapshots",
                    "total_boardings",
                    "unique_cards",
                    "source_checks",
                    "notes",
                    "attribution",
                )
            },
            "sources": json.loads((r.folder / "sources.json").read_bytes()),
            "experiments": records(r.frame("forecast_summary")),
            "experiment_metrics": records(r.frame("forecast_metrics")),
            "experiment_intervals": records(r.frame("forecast_bootstrap")),
            "fares": records(r.frame("fare_dictionary")),
        }

    def query(self, spec):
        r = self.current()
        filters = spec.model_dump(mode="json")

        def empty(reason, status="unavailable"):
            return {"meta": r.meta(filters, status, reason), "rows": [], "summary": [], "total": None}

        if not spec.route_ids:
            return empty("Выберите маршруты")
        if set(spec.route_ids) - set(r.manifest["routes"]):
            return empty("Для выбранных маршрутов нет категорий")
        start, end = pd.Timestamp(spec.time_range.start), pd.Timestamp(spec.time_range.end)
        coverage = r.manifest["coverage"]
        if (
            spec.mode == "forecast"
            or start < pd.Timestamp(coverage["start"])
            or end > pd.Timestamp(coverage["end"])
        ):
            return empty("Категориального прогноза нет. История доступна за январь–октябрь 2025 года.")
        times = pd.date_range(start, end, inclusive="left", freq="h")
        grid = pd.MultiIndex.from_product([spec.route_ids, times], names=["route", "timestamp"])
        selection = [("route", "in", spec.route_ids), ("timestamp", ">=", start), ("timestamp", "<", end)]
        total = r.frame("route_hour_total", selection)
        expected = total.set_index(["route", "timestamp"]).boardings.reindex(grid, fill_value=0)
        snapshot = self.store.read("snapshots", spec.snapshot_id)
        if not snapshot.get("historyId"):
            return empty("В выбранном снимке нет истории", "incompatible")
        history = self.store.read("histories", snapshot["historyId"])
        if history.get("datasetId"):
            actual = self.data.frame(history["datasetId"], route_ids=spec.route_ids, start=start, end=end)
        else:
            actual = pd.read_parquet(
                self.store.path("histories", snapshot["historyId"], "parquet"),
                columns=["route", "timestamp", "value"],
            )
        actual.route = actual.route.astype(str)
        actual = actual.set_index(["route", "timestamp"]).value.reindex(grid)
        if not actual.notna().all() or not np.array_equal(actual.to_numpy(), expected.to_numpy()):
            return empty("Категории не соответствуют истории выбранного снимка", "incompatible")
        frame = r.frame("route_hour_category", selection)
        categories = spec.categories or list(CATEGORIES)
        cat_grid = pd.MultiIndex.from_product(
            [spec.route_ids, times, categories], names=["route", "timestamp", "category"]
        )
        frame = frame.set_index(["route", "timestamp", "category"])[["boardings", "unique_cards"]]
        frame = frame.reindex(cat_grid, fill_value=0).reset_index()
        if spec.grain == "day":
            frame.timestamp = frame.timestamp.dt.floor("D")
            frame = frame.groupby(["route", "timestamp", "category"], as_index=False).boardings.sum()
            frame["unique_cards"] = None
        # Denominator always includes all ticket categories, including filtered-out ones.
        denominator = expected.reset_index(name="all_boardings")
        if spec.grain == "day":
            denominator.timestamp = denominator.timestamp.dt.floor("D")
            denominator = denominator.groupby(["route", "timestamp"], as_index=False).all_boardings.sum()
        frame = frame.merge(denominator, on=["route", "timestamp"], validate="many_to_one")
        frame["share"] = frame.boardings.div(frame.all_boardings.replace(0, np.nan))
        summary = frame.groupby("category", as_index=False).boardings.sum()
        grand_total = int(expected.sum())
        summary["share"] = summary.boardings / grand_total if grand_total else np.nan
        summary["unique_cards"] = None
        if len(times) == 1 and len(spec.route_ids) == 1:
            summary["unique_cards"] = summary.category.map(frame.set_index("category").unique_cards)
        return {
            "meta": r.meta(filters),
            "rows": records(frame),
            "summary": records(summary),
            "total": grand_total,
        }

    def seasonality(self, route_ids):
        r = self.current()
        if set(route_ids) - set(r.manifest["routes"]):
            raise DomainError("PASSENGER_ROUTE_UNKNOWN", "Для маршрута нет сезонных сводок")
        # Monthly/card profiles are network-wide. Route comparisons remain separate rows.
        comparison = (
            r.frame("seasonal_contributions_by_route", [("route", "in", route_ids)])
            if route_ids
            else r.frame("seasonal_contributions")
        )
        return {
            "meta": r.meta(
                {
                    "comparison_route_ids": route_ids,
                    "profiles_scope": "network",
                    "period": "2025-01-01/2025-11-01",
                }
            ),
            "monthly": records(r.frame("category_month_profiles")),
            "hourly": records(r.frame("category_hour_profiles")),
            "weekdays": records(r.frame("category_dow_profiles")),
            "comparisons": records(comparison),
        }

    def cohorts(self):
        r = self.current()
        return {
            "meta": r.meta(
                {
                    "scope": "network",
                    "cohort_period": "2025-04-01/2025-06-01",
                    "observation_period": "2025-04-01/2025-11-01",
                }
            ),
            "monthly": records(r.frame("fixed_cohort_summary")),
            "autumn": records(r.frame("autumn_return_total")),
            "autumn_categories": records(r.frame("autumn_return_summary")),
            "category_changes": records(r.frame("cohort_category_changes")),
        }

    @staticmethod
    def month(r, day):
        date = str(day)
        if not r.manifest["coverage"]["start"][:10] <= date < r.manifest["coverage"]["end"][:10]:
            return None
        return next((m for m in reversed(r.manifest["snapshots"]) if m <= date), None)

    def network(self, snapshot_id, day):
        snapshot = self.store.read("snapshots", snapshot_id)
        return resolve_network(self.store, self.store.read("networks", snapshot["networkId"]), str(day))

    @staticmethod
    def stop_matches(row, stop):
        return (
            str(row.route) == stop["routeId"]
            and row.stop_occurrence_id == stop["id"]
            and row.stop_id == stop["stationId"]
            and round(row.longitude, 6) == round(stop["coordinates"][0], 6)
            and round(row.latitude, 6) == round(stop["coordinates"][1], 6)
        )

    def compatible_routes(self, r, network, month, routes):
        stops = r.frame("stop_infrastructure_monthly", [("snapshot", "==", month), ("route", "in", routes)])
        good = []
        for route in routes:
            present = {s["id"]: s for s in network["stops"] if s["routeId"] == route}
            original = stops[stops.route.eq(route)]
            if (
                len(original)
                and len(present) == len(original)
                and all(
                    row.stop_occurrence_id in present
                    and self.stop_matches(row, present[row.stop_occurrence_id])
                    for row in original.itertuples()
                )
            ):
                good.append(route)
        return good

    def infrastructure(self, snapshot_id, day, route, radius, stop_id=None):
        r = self.current()
        month = self.month(r, day)
        filters = {
            "snapshot_id": snapshot_id,
            "date": str(day),
            "route": route,
            "radius": radius,
            "stop_id": stop_id,
            "source_snapshot": month,
        }
        empty = {"meta": r.meta(filters, "unavailable", "На эту дату нет инфраструктуры"), "rows": []}
        if month is None:
            return empty
        network = self.network(snapshot_id, day)
        if stop_id:
            frame = r.frame(
                "stop_infrastructure_monthly",
                [("snapshot", "==", month), ("route", "==", route), ("stop_occurrence_id", "==", stop_id)],
            )
            stop = next((s for s in network["stops"] if s["id"] == stop_id and s["routeId"] == route), None)
            if frame.empty or stop is None or not self.stop_matches(next(frame.itertuples()), stop):
                empty["meta"] = r.meta(filters, "incompatible", "Нет совпадающей остановки в месячном срезе")
                return empty
        else:
            if route not in self.compatible_routes(r, network, month, [route]):
                empty["meta"] = r.meta(filters, "incompatible", "Нет инфраструктуры для этой версии маршрута")
                return empty
            frame = r.frame(
                "route_infrastructure_monthly", [("snapshot", "==", month), ("route", "==", route)]
            )
        suffix = f"_{radius}m"
        counts = [
            {"kind": c.removesuffix(suffix), "value": v}
            for c, v in records(frame)[0].items()
            if c.endswith(suffix)
        ]
        return {
            "meta": r.meta(filters),
            "rows": counts,
            "source": records(frame)[0],
            "attribution": r.manifest["attribution"],
        }

    def poi(self, snapshot_id, day, routes, categories, radius, bbox):
        r = self.current()
        month = self.month(r, day)
        filters = {
            "snapshot_id": snapshot_id,
            "date": str(day),
            "route_ids": routes,
            "categories": categories,
            "radius": radius,
            "bbox": bbox,
            "source_snapshot": month,
        }
        empty = {
            "meta": r.meta(filters, "unavailable", "Нет объектов для выбранной даты и маршрутов"),
            "type": "FeatureCollection",
            "features": [],
        }
        if month is None or not routes:
            return empty
        compatible = self.compatible_routes(r, self.network(snapshot_id, day), month, routes)
        filters["unavailable_route_ids"] = sorted(set(routes) - set(compatible))
        if not compatible:
            return empty
        selection = [("route", "in", compatible), ("distance_m", "<=", radius)]
        if categories:
            selection.append(("category", "in", categories))
        links = r.frame("stop_poi_links_" + month, selection, ["object_id", "route", "distance_m"])
        if links.empty:
            return empty
        objects = r.frame("poi_" + month, [("object_id", "in", links.object_id.unique().tolist())])
        if bbox:
            objects = objects[
                objects.longitude.between(bbox[0], bbox[2]) & objects.latitude.between(bbox[1], bbox[3])
            ]
        links = links.groupby("object_id").agg(
            distance_m=("distance_m", "min"), route_ids=("route", lambda x: sorted(set(x)))
        )
        objects = objects.merge(links, on="object_id", validate="one_to_one")
        features = []
        for row in records(objects):
            lon, lat = row.pop("longitude"), row.pop("latitude")
            features.append(
                {
                    "type": "Feature",
                    "id": row["object_id"],
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": row,
                }
            )
        return {
            "meta": r.meta(filters),
            "type": "FeatureCollection",
            "features": features,
            "attribution": r.manifest["attribution"],
        }
