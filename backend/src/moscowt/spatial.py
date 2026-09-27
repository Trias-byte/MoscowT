"""Conservative spatial scenarios. Stop boardings conserve route totals; segment traversals do not."""

import math
from collections import defaultdict
from functools import lru_cache

import pandas as pd

from .domain import DomainError
from .network_history import resolve_network

VERSION = "ordered-stop-scenario-v2"


def meters(a, b):
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    delta = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    )
    return 12742000 * math.asin(min(1, math.sqrt(delta)))


def phase(stamp):
    stamp = pd.Timestamp(stamp).tz_convert("Europe/Moscow")
    if stamp.dayofweek < 5:
        if 7 <= stamp.hour < 11:
            return "morning"
        if 16 <= stamp.hour < 20:
            return "evening"
    return "neutral"


class SpatialService:
    def __init__(self, store, data_root=None):
        self.store, self.data_root = store, data_root

    @lru_cache(maxsize=16)
    def compile(self, network_id, day):
        network = resolve_network(self.store, self.store.read("networks", network_id), day)
        transfers = defaultdict(set)
        for stop in network["stops"]:
            transfers[stop["stationId"]].add(stop["routeId"])
        routes = defaultdict(list)
        for pattern in network["patterns"]:
            stops = sorted(
                (s for s in network["stops"] if s["patternId"] == pattern["id"]), key=lambda s: s["order"]
            )
            segments = sorted(
                (s for s in network["segments"] if s["patternId"] == pattern["id"]), key=lambda s: s["order"]
            )
            if (
                len(stops) < 2
                or len(segments) != len(stops) - 1
                or any(
                    s["fromId"] != stops[i]["id"] or s["toId"] != stops[i + 1]["id"]
                    for i, s in enumerate(segments)
                )
            ):
                continue
            distances = [0.0]
            for segment in segments:
                points = segment["coordinates"]
                distances.append(distances[-1] + sum(meters(a, b) for a, b in zip(points, points[1:])))
            if not distances[-1]:
                continue
            attraction = [
                1
                + (2 if any(word in s["name"].lower() for word in ("метро", "мцк", "мцд", "вокзал")) else 0)
                + min(2, 0.4 * (len(transfers[s["stationId"]]) - 1))
                for s in stops
            ]
            routes[pattern["routeId"]].append(
                {
                    **pattern,
                    "stops": stops,
                    "segments": segments,
                    "distance": distances,
                    "attraction": attraction,
                }
            )
        return {
            rid: patterns
            for rid, patterns in routes.items()
            if len(patterns) == 2 and len({p["direction"] for p in patterns}) == 2
        }

    @lru_cache(maxsize=256)
    def profile(self, network_id, day, rid, time_phase):
        profiles, total = {}, 0.0
        for pattern in self.compile(network_id, day).get(rid, []):
            n = len(pattern["stops"])
            boardings, traversals = [0.0] * n, [0.0] * (n - 1)
            for first in range(n - 1):
                for last in range(first + 1, n):
                    a, b = pattern["attraction"][first], pattern["attraction"][last]
                    origin = 1 if time_phase == "morning" else a if time_phase == "evening" else math.sqrt(a)
                    destination = (
                        1 if time_phase == "evening" else b if time_phase == "morning" else math.sqrt(b)
                    )
                    weight = (
                        origin
                        * destination
                        * math.exp(-(pattern["distance"][last] - pattern["distance"][first]) / 4000)
                    )
                    boardings[first] += weight
                    for segment in range(first, last):
                        traversals[segment] += weight
                    total += weight
            profiles[pattern["id"]] = {"stop": boardings, "segment": traversals}
        return (
            {
                pid: {kind: [v / total for v in values] for kind, values in profile.items()}
                for pid, profile in profiles.items()
            }
            if total
            else {}
        )

    def direction_hours(self, snapshot, route, pattern, length, start, end, fleet, additional=0):
        share = pattern["distance"][-1] / length
        vehicle_hours = fleet * share if fleet is not None else None
        if snapshot.get("scheduleId") and self.data_root:
            from .domain import TimeRange
            from .schedules import ScheduleService, schedule_table

            service = ScheduleService(self.data_root)
            trips = schedule_table(service.store.root, snapshot["scheduleId"])
            names = set(trips.loc[trips.route == route, "direction"])
            # Exact names establish the link; duty/route numbers never imply direction.
            if pattern["name"] in names:
                scheduled = service.hours(
                    snapshot["scheduleId"],
                    TimeRange(start=start, end=end),
                    [route],
                    direction=pattern["name"],
                    scenario=snapshot.get("scheduleScenario", False),
                )
                base = scheduled[0]["vehicle_hours"]
                return (
                    base + additional * share if base is not None else None
                ), "schedule_direction_with_budget_length_share"
        return vehicle_hours, "route_length_share_assumption"

    def sections(self, snapshot, frames):
        result = {}
        for frame in frames:
            stamp = frame["start"]
            day = pd.Timestamp(stamp).tz_convert("Europe/Moscow").strftime("%Y-%m-%d")
            routes = self.compile(snapshot["networkId"], day)
            for row in frame["values"]:
                rid = row["routeId"]
                patterns = routes.get(rid, [])
                profiles = self.profile(snapshot["networkId"], day, rid, phase(stamp))
                length = sum(p["distance"][-1] for p in patterns)
                for pattern in patterns:
                    vehicle_hours, denominator = self.direction_hours(
                        snapshot,
                        rid,
                        pattern,
                        length,
                        frame["start"],
                        frame["end"],
                        row.get("vehicleHours"),
                        row.get("additionalVehicleHours", 0),
                    )
                    for index, segment in enumerate(pattern["segments"]):
                        flow = (
                            row["value"] * profiles[pattern["id"]]["segment"][index]
                            if row["value"] is not None
                            else None
                        )
                        item = result.setdefault(
                            segment["id"],
                            {
                                "segmentId": segment["id"],
                                "routeId": rid,
                                "patternId": pattern["id"],
                                "fromName": pattern["stops"][index]["name"],
                                "toName": pattern["stops"][index + 1]["name"],
                                "directionName": pattern["name"],
                                "estimatedFlow": 0.0,
                                "vehicleHours": 0.0,
                                "hours": 0,
                                "modelVersion": VERSION,
                                "denominatorMethod": denominator,
                            },
                        )
                        for key, value in (("estimatedFlow", flow), ("vehicleHours", vehicle_hours)):
                            item[key] = (
                                item[key] + value if item[key] is not None and value is not None else None
                            )
                        item["hours"] += 1
        for item in result.values():
            item["rate"] = (
                item["estimatedFlow"] / item["vehicleHours"]
                if item["estimatedFlow"] is not None and item["vehicleHours"]
                else None
            )
            item["meanVehicles"] = (
                item["vehicleHours"] / item["hours"] if item["vehicleHours"] is not None else None
            )
            item["source"] = "scenario" if item["rate"] is not None else "missing"
        return result

    def frame(self, route_frame, spec):
        snapshot = {
            **self.store.read("snapshots", spec.snapshot_id),
            "scheduleId": spec.schedule_id,
            "scheduleScenario": spec.schedule_scenario,
        }
        rows = []
        for row in route_frame.itertuples():
            day = row.timestamp.strftime("%Y-%m-%d")
            patterns = self.compile(snapshot["networkId"], day).get(row.route, [])
            profile = self.profile(snapshot["networkId"], day, row.route, phase(row.timestamp))
            if not patterns:
                raise DomainError(
                    "SPATIAL_COVERAGE_MISSING", f"Нет применимой геометрии маршрута {row.route} на {day}"
                )
            length = sum(p["distance"][-1] for p in patterns)
            for pattern in patterns:
                fleet = None if pd.isna(row.vehicle_hours) else row.vehicle_hours
                vehicle_hours, denominator = self.direction_hours(
                    snapshot,
                    row.route,
                    pattern,
                    length,
                    row.timestamp,
                    row.timestamp + pd.Timedelta(hours=1),
                    fleet,
                    row.additional_vehicle_hours,
                )
                objects = pattern["stops"] if spec.metric_scope == "stop" else pattern["segments"]
                for index, obj in enumerate(objects):
                    if spec.object_ids and obj["id"] not in spec.object_ids:
                        continue
                    share = profile[pattern["id"]][spec.metric_scope][index]
                    rows.append(
                        {
                            **route_frame.attrs.get("lineage", {}),
                            "route": row.route,
                            "timestamp": row.timestamp,
                            "object_id": obj["id"],
                            "value": row.value * share,
                            "base_value": row.base_value * share,
                            "metric": "stop_boardings_estimate"
                            if spec.metric_scope == "stop"
                            else "segment_traversals_estimate",
                            "method": VERSION,
                            "unit": "estimated_boardings"
                            if spec.metric_scope == "stop"
                            else "estimated_traversals",
                            "vehicle_hours": vehicle_hours if spec.metric_scope == "segment" else None,
                            "denominator_method": denominator
                            if spec.metric_scope == "segment"
                            else "not_applicable",
                            "provenance": "scenario",
                            "dataset_id": spec.dataset_id,
                            "forecast_id": spec.forecast_id,
                            "scenario_id": spec.scenario_id,
                        }
                    )
        if not rows:
            raise DomainError(
                "SPATIAL_COVERAGE_MISSING", "Нет полной применимой геометрии для выбранных объектов"
            )
        result = pd.DataFrame(rows)
        if spec.grain != "hour":
            from .platform_exports import aggregate_timestamp

            result.timestamp = aggregate_timestamp(result.timestamp, spec.grain)
            keys = [c for c in result.columns if c not in ("value", "base_value", "vehicle_hours")]
            result = result.groupby(keys, dropna=False, as_index=False)[
                ["value", "base_value", "vehicle_hours"]
            ].agg(lambda values: values.sum() if values.notna().all() else float("nan"))
        result["validations_per_vehicle_hour"] = result.value / result.vehicle_hours.where(
            result.vehicle_hours > 0
        )
        return result
