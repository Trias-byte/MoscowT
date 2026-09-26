from collections import OrderedDict
from datetime import datetime, timedelta
from math import isfinite

import numpy as np
import pandas as pd

from .domain import METRIC, ROUTES, DomainError, Scope
from .fleet import FleetService, fleet_summary
from .storage import SnapshotStore


def number(value):
    return float(value) if isfinite(value) else None


def complete_sum(values):
    return number(np.sum(values))


def provenance(sources):
    present = set(sources) - {0}
    if present == {1, 2}:
        return "mixed"
    return "observation" if present == {1} else "forecast" if present == {2} else "missing"


class RouteAnalyticsService:
    """Small immutable matrices, bounded by three loaded data versions."""

    def __init__(self, store: SnapshotStore):
        self.store = store
        self.fleet = FleetService(store)
        self.matrices = OrderedDict()

    def matrix(self, kind, ident):
        key = (kind, ident)
        if key in self.matrices:
            self.matrices.move_to_end(key)
            return self.matrices[key]
        manifest = self.store.read(kind, ident)
        frame = pd.read_parquet(self.store.path(kind, ident, "parquet"))
        frame = frame.sort_values(["route", "timestamp"])
        start, end = datetime.fromisoformat(manifest["start"]), datetime.fromisoformat(manifest["end"])
        count = int((end - start).total_seconds() // 3600)
        if len(frame) != len(ROUTES) * count:
            raise DomainError("ARTIFACT_INVALID", "Неполная сетка артефакта", 503)
        result = {"start": start, "end": end, "count": count, "manifest": manifest}
        for col in ("value", "baseline", "baselineCount", "valueOrigin", "coverageStatus", "qualityFlag"):
            array = frame[col].to_numpy().reshape(len(ROUTES), count)
            array.flags.writeable = False
            result[col] = array
        self.matrices[key] = result
        while len(self.matrices) > 3:
            self.matrices.popitem(last=False)
        return result

    def view(self, scope: Scope):
        snapshot = self.store.read("snapshots", scope.snapshotId)
        # Load forecasts first so available observations (including zeros) take priority.
        matrices = []
        for mode, kind, field, source in (
            ("forecast", "forecasts", "forecastId", 2),
            ("history", "histories", "historyId", 1),
        ):
            if scope.mode in ("auto", mode) and snapshot.get(field):
                matrices.append((source, self.matrix(kind, snapshot[field])))
        if not matrices:
            raise DomainError(
                "FORECAST_NOT_READY" if scope.mode == "forecast" else "DATA_NOT_READY",
                "Для выбранного снимка данные ещё не подготовлены",
                409,
            )
        run = next((matrix["manifest"] for source, matrix in matrices if source == 2), None)
        hours = 1 if scope.grain == "hour" else 24
        frame_count = int((scope.timeRange.end - scope.timeRange.start).total_seconds() // (hours * 3600))
        starts = [scope.timeRange.start + timedelta(hours=i * hours) for i in range(frame_count)]
        slices = []
        for source, matrix in matrices:
            indices = int((scope.timeRange.start - matrix["start"]).total_seconds() // 3600) + np.arange(
                frame_count * hours
            )
            present = (indices >= 0) & (indices < matrix["count"])
            slices.append((source, matrix, indices, present))
        selected_routes = [int(r) for r in scope.routeIds]
        series, route_values = [], {}
        used_sources = set()
        for route in selected_routes:
            row = ROUTES.index(route)
            numeric = {
                name: np.full(frame_count * hours, np.nan) for name in ("value", "baseline", "baselineCount")
            }
            origins_by_hour = np.full(frame_count * hours, "", dtype=object)
            flags_by_hour = np.full(frame_count * hours, "none", dtype=object)
            sources = np.zeros(frame_count * hours, dtype=np.uint8)
            for source, matrix, indices, present in slices:
                positions = np.flatnonzero(present)
                positions = positions[np.isfinite(matrix["value"][row, indices[positions]])]
                for name, expanded in numeric.items():
                    expanded[positions] = matrix[name][row, indices[positions]]
                origins_by_hour[positions] = matrix["valueOrigin"][row, indices[positions]]
                flags_by_hour[positions] = matrix["qualityFlag"][row, indices[positions]]
                sources[positions] = source
            used_sources.update(sources.tolist())
            values, baseline, counts = [], [], []
            for name, target in (("value", values), ("baseline", baseline), ("baselineCount", counts)):
                grouped = numeric[name].reshape(frame_count, hours)
                target.extend(
                    [
                        number(x)
                        for x in (grouped.min(axis=1) if name == "baselineCount" else grouped.sum(axis=1))
                    ]
                )
            quality = []
            # Fetch whole slices once. Per-hour NumPy calls release the GIL repeatedly
            # and cause thread contention on a cold request mix.
            origins_by_hour = origins_by_hour.tolist()
            flags_by_hour = flags_by_hour.tolist()
            sources = sources.tolist()
            fleet_hours = self.fleet.hours(
                snapshot, scope.timeRange.start, str(route), sources, run["issuedAt"] if run else None
            )
            for i in range(frame_count):
                positions = [j for j in range(i * hours, (i + 1) * hours) if sources[j]]
                origins = sorted({origins_by_hour[j] for j in positions})
                flags = sorted({flags_by_hour[j] for j in positions} - {"none"})
                source = provenance(sources[j] for j in positions)
                quality.append(
                    {
                        "routeId": str(route),
                        "provenance": source,
                        "value": values[i],
                        "baseline": baseline[i],
                        "baselineCount": counts[i],
                        "valueOrigins": origins,
                        "qualityFlags": flags,
                        "coverageStatus": ("provided_extract" if source == "observation" else source)
                        if values[i] is not None
                        else "missing",
                        "historySupport": "no_positive_history" if route == 5 else "available",
                        **fleet_summary(fleet_hours[i * hours : (i + 1) * hours], values[i]),
                    }
                )
            route_values[route] = quality
            series.append({"routeId": str(route), "points": values, "baseline": baseline})
        frames = []
        for i, start in enumerate(starts):
            vals = [route_values[route][i] for route in selected_routes]
            frames.append(
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(hours=hours)).isoformat(),
                    "values": vals,
                    "aggregate": sum(v["value"] for v in vals)
                    if all(v["value"] is not None for v in vals)
                    else None,
                    "baseline": sum(v["baseline"] for v in vals)
                    if all(v["baseline"] is not None for v in vals)
                    else None,
                }
            )
        if 2 not in used_sources:
            run = None
        meta = {
            "contractVersion": 2,
            "snapshotId": scope.snapshotId,
            "networkSnapshotId": snapshot.get("networkId"),
            "historyId": snapshot["historyId"],
            "fleetId": snapshot.get("fleetId"),
            "fleetMethod": self.store.read("fleets", snapshot["fleetId"]).get("method")
            if snapshot.get("fleetId")
            else None,
            "metric": METRIC,
            "metricScope": "route",
            "unit": "validations",
            "intervalUnit": "validations/hour" if hours == 1 else "validations/day",
            "aggregation": "sum",
            "grain": scope.grain,
            "timeRange": scope.timeRange.model_dump(mode="json"),
            "provenance": provenance(used_sources),
            "forecastId": run["id"] if run else None,
            "modelId": run["modelId"] if run else None,
            "issuedAt": run["issuedAt"] if run else None,
            "createdAt": run["createdAt"] if run else None,
            "availabilityPolicy": "retrospective_event_time",
            "geometryFilterAffectsMetric": False,
            "baselineDescription": "Среднее до момента выпуска по тем же дням недели и часам за 8 недель"
            if run
            else "Среднее по предыдущим восьми таким дням недели и часам",
            "coverageNote": "Успехи в предоставленной выгрузке; полнота источника не подтверждена",
        }
        comparison = []
        for s in series:
            total = sum(s["points"]) if all(v is not None for v in s["points"]) else None
            base = sum(s["baseline"]) if all(v is not None for v in s["baseline"]) else None
            comparison.append(
                {
                    "routeId": s["routeId"],
                    "value": total,
                    "baseline": base,
                    "difference": total - base if total is not None and base is not None else None,
                }
            )
        return {
            "meta": meta,
            "frames": frames,
            "series": series,
            "comparison": comparison,
            "heatmap": {
                "routeIds": scope.routeIds,
                "cells": [[i, r, v] for r, s in enumerate(series) for i, v in enumerate(s["points"])],
            },
        }


def segment_intersects_bbox(a, b, box):
    """Liang–Barsky clipping predicate also catches crossings without inner vertices."""
    lo, hi = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - box[0]), (dx, box[2] - a[0]), (-dy, a[1] - box[1]), (dy, box[3] - a[1])):
        if p == 0:
            if q < 0:
                return False
        elif p < 0:
            lo = max(lo, q / p)
        else:
            hi = min(hi, q / p)
        if lo > hi:
            return False
    return True


class NetworkCatalogService:
    def __init__(self, store: SnapshotStore):
        self.store = store

    def geometry(self, scope: Scope):
        snapshot = self.store.read("snapshots", scope.snapshotId)
        if not snapshot.get("networkId"):
            raise DomainError("NETWORK_NOT_READY", "Справочник не подготовлен", 503)
        from .network_history import resolve_network

        archive = self.store.read("networks", snapshot["networkId"])
        day = scope.geometry.date or scope.timeRange.start.date()
        network = resolve_network(self.store, archive, day.isoformat())
        available = {p["id"]: p for p in network["patterns"]}
        wanted = scope.geometry.patternIds
        if any(pid not in available for pid in wanted):
            raise DomainError("PATTERN_NOT_FOUND", "Неизвестный шаблон направления")
        patterns = [
            p
            for p in network["patterns"]
            if p["routeId"] in scope.routeIds and (not wanted or p["id"] in wanted)
        ]
        hidden = []
        if scope.geometry.referenceMode == "historical" and not archive.get("versions"):
            start, end = scope.timeRange.start.date().isoformat(), scope.timeRange.end.date().isoformat()
            for p in patterns:
                if start < max(p["observedAt"], p["validFrom"]) or (p["validTo"] and end > p["validTo"]):
                    hidden.append(p["id"])
            patterns = [p for p in patterns if p["id"] not in hidden]
        section = scope.geometry.section
        orders = None
        if section:
            visits = {s["id"]: s for s in network["stops"] if s["patternId"] == section.patternId}
            if (
                section.patternId not in available
                or section.fromId not in visits
                or section.toId not in visits
            ):
                raise DomainError("INVALID_SECTION", "Границы участка должны принадлежать одному шаблону")
            first, last = visits[section.fromId]["order"], visits[section.toId]["order"]
            if first >= last or available[section.patternId]["routeId"] not in scope.routeIds:
                raise DomainError("INVALID_SECTION", "Неверный порядок остановок или маршрут участка")
            patterns = [p for p in patterns if p["id"] == section.patternId]
            orders = (first, last)
        pids = {p["id"] for p in patterns}
        stops = [
            s
            for s in network["stops"]
            if s["patternId"] in pids and (not orders or orders[0] <= s["order"] <= orders[1])
        ]
        segments = [
            s
            for s in network["segments"]
            if s["patternId"] in pids and (not orders or orders[0] <= s["order"] < orders[1])
        ]
        b = scope.geometry.bbox
        if b:
            stops = [
                s
                for s in stops
                if b[0] <= s["coordinates"][0] <= b[2] and b[1] <= s["coordinates"][1] <= b[3]
            ]
            segments = [
                s
                for s in segments
                if any(
                    segment_intersects_bbox(first, second, b)
                    for first, second in zip(s["coordinates"], s["coordinates"][1:])
                )
            ]
        return {
            "networkSnapshotId": network["networkSnapshotId"],
            "patternIds": sorted(pids),
            "stopIds": [s["id"] for s in stops],
            "segmentIds": [s["id"] for s in segments],
            "geometryQuality": network["geometryQuality"],
            "asOf": network["asOf"],
            "referenceMode": scope.geometry.referenceMode,
            "historicallyUnavailablePatternIds": hidden,
            "missingGeometryRouteIds": [r for r in scope.routeIds if r in network["missingGeometryRouteIds"]],
            "geometryFilterAffectsMetric": False,
            "warning": network["warning"],
        }
