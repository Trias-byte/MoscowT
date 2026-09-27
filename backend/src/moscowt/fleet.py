"""Provisional active-vehicle estimates from distinct garage numbers, not validators."""

import json
import math
from datetime import datetime, timedelta
from functools import lru_cache
from math import floor
from statistics import median

from .constants.fleet import (
    VEHICLE_LOAD_THRESHOLDS as VEHICLE_LOAD_THRESHOLDS,
)
from .domain import HISTORY_END, HISTORY_START, ROUTES, TZ, DomainError
from .service_calendar import profile_weekday
from .storage import digest


def prepare_fleet(store, dataset):
    source = dataset / "derived/fleet/observed-activity.json"
    if not source.exists():
        source = dataset / "derived/fleet/observed-fleet.json"
    if not source.exists():
        raise DomainError("FLEET_NOT_PREPARED", "Сначала выполните build_fleet_observations.py", 409)
    raw = json.loads(source.read_bytes())
    size = int((HISTORY_END - HISTORY_START).total_seconds() / 3600)
    if (
        raw.get("schemaVersion") not in (1, 2)
        or datetime.fromisoformat(raw["start"]) != HISTORY_START
        or datetime.fromisoformat(raw["end"]) != HISTORY_END
        or set(raw["routes"]) != {str(r) for r in ROUTES}
    ):
        raise ValueError("Invalid fleet observation coverage")
    for series in raw["routes"].values():
        if any(len(series[key]) != size for key in ("vehicles", "events", "identifiedEvents")):
            raise ValueError("Incomplete fleet grid")
        for count, events, known in zip(series["vehicles"], series["events"], series["identifiedEvents"]):
            if (
                type(count) not in (int, float)
                or not math.isfinite(count)
                or not all(type(n) is int for n in (events, known))
                or not 0 <= count <= known <= events
            ):
                raise ValueError("Invalid fleet count or identifier coverage")
        if raw["schemaVersion"] == 2:
            for key in ("directActivityVehicles", "peakFiveMinuteVehicles", "hourlyDistinctVehicles"):
                if len(series.get(key, [])) != size:
                    raise ValueError("Incomplete activity diagnostics")
            for direct, estimate, peak, unique in zip(
                series["directActivityVehicles"],
                series["vehicles"],
                series["peakFiveMinuteVehicles"],
                series["hourlyDistinctVehicles"],
            ):
                if not 0 <= direct <= estimate <= peak <= unique:
                    raise ValueError("Invalid activity diagnostic ordering")
    history_id = store.current()["historyId"]
    payload = {
        **raw,
        "historyId": history_id,
        "estimatorVersion": raw["schemaVersion"],
        "profileWeeks": 8,
        "minimumProfileDays": 3,
    }
    fleet_id = "fleet-" + digest(payload)
    store.put("fleets", fleet_id, {"id": fleet_id, **payload})
    store.publish(fleetId=fleet_id)
    return {"fleetId": fleet_id, "historyId": history_id, "inputs": raw["inputs"]}


def fleet_summary(records, value):
    """Sum vehicle-hours before dividing; never average the per-hour ratios."""
    counts = [r["vehicles"] for r in records]
    vehicle_hours = sum(counts) if records and all(c is not None for c in counts) else None
    sources = {r["source"] for r in records}
    source = (
        "missing"
        if "missing" in sources or not sources
        else next(iter(sources))
        if len(sources) == 1
        else "mixed"
    )
    coverages = [r["coverage"] for r in records if r["coverage"] is not None]
    return {
        "fleetVehicles": vehicle_hours / len(records) if vehicle_hours is not None else None,
        "vehicleHours": vehicle_hours,
        "loadPerVehicleHour": value / vehicle_hours
        if value is not None and vehicle_hours and vehicle_hours > 0
        else None,
        "fleetSource": source,
        "fleetSampleDays": min((r["sampleDays"] for r in records), default=0),
        "fleetCoverage": min(coverages) if coverages else None,
    }


def missing():
    return {"vehicles": None, "source": "missing", "sampleDays": 0, "coverage": None}


class FleetService:
    def __init__(self, store, data_root=None):
        self.store, self.data_root = store, data_root

    def observed(self, fleet_id, route, timestamp):
        data = self.store.read("fleets", fleet_id)
        offset = (timestamp - datetime.fromisoformat(data["start"])).total_seconds() / 3600
        series = data["routes"].get(route)
        if series is None or not offset.is_integer() or not 0 <= offset < len(series["vehicles"]):
            return missing()
        index = int(offset)
        events, known = series["events"][index], series["identifiedEvents"][index]
        if events and not known:
            return {**missing(), "coverage": 0}
        return {
            "vehicles": series["vehicles"][index],
            "source": "observed",
            "sampleDays": 1,
            "coverage": known / events if events else None,
        }

    @lru_cache(maxsize=4096)
    def profile(self, fleet_id, route, weekday, hour, issued_at):
        data = self.store.read("fleets", fleet_id)
        cutoff = min(datetime.fromisoformat(issued_at).astimezone(TZ), datetime.fromisoformat(data["end"]))
        candidate = cutoff.replace(hour=hour, minute=0, second=0, microsecond=0)
        candidate -= timedelta(days=(candidate.weekday() - weekday) % 7)
        # Only completed hours are available to this issue, even for intrahour cutoffs.
        available_at = candidate + timedelta(hours=1)
        if data.get("schemaVersion", 1) == 2 and available_at.hour != 0:
            # A short gap can be confirmed by a later bin in the next hour.
            # Wait for that evidence too. The scanner never fills across midnight.
            available_at += timedelta(minutes=data.get("maximumGapMinutes", 20))
        if available_at > cutoff:
            candidate -= timedelta(days=7)
        records = [
            self.observed(fleet_id, route, candidate - timedelta(weeks=i))
            for i in range(8)
            if data.get("schemaVersion", 1) == 1
            or profile_weekday((candidate - timedelta(weeks=i)).date()) == weekday
        ]
        known = [r for r in records if r["vehicles"] is not None]
        if len(known) < 3:
            return missing()
        # A known route may have no validations in its night hours. Preserve those
        # zeros so they do not invalidate an otherwise complete daily denominator.
        # Without any vehicle evidence for this route, zero is not a fleet estimate.
        history_start = datetime.fromisoformat(data["start"])
        end_index = max(0, floor((cutoff - history_start).total_seconds() / 3600))
        route_counts = data["routes"].get(route, {}).get("vehicles", [])
        if not any(route_counts[max(0, end_index - 8 * 7 * 24) : end_index]):
            return missing()
        coverage = [r["coverage"] for r in known if r["coverage"] is not None]
        return {
            "vehicles": (
                median(r["vehicles"] for r in known)
                if data.get("schemaVersion", 1) == 2
                else floor(median(r["vehicles"] for r in known) + 0.5)
            ),
            "source": "estimated",
            "sampleDays": len(known),
            "coverage": min(coverage) if coverage else None,
        }

    def hours(self, snapshot, start, route, sources, issued_at):
        if snapshot.get("scheduleId") and self.data_root:
            from .domain import TimeRange
            from .schedules import ScheduleService

            records = ScheduleService(self.data_root).hours(
                snapshot["scheduleId"],
                TimeRange(start=start, end=start + timedelta(hours=len(sources))),
                [route],
                scenario=snapshot.get("scheduleScenario", False),
            )
            return [
                {
                    "vehicles": r["vehicle_hours"],
                    "source": r["method"],
                    "sampleDays": 0,
                    "coverage": 1.0 if r["vehicle_hours"] is not None else None,
                }
                for r in records
            ]
        fleet_id = snapshot.get("fleetId")
        if not fleet_id:
            return [missing() for _ in sources]
        data = self.store.read("fleets", fleet_id)
        if data["historyId"] != snapshot["historyId"]:
            raise DomainError("FLEET_HISTORY_MISMATCH", "Оценка вагонов относится к другой истории", 409)
        records = []
        for index, source in enumerate(sources):
            timestamp = start + timedelta(hours=index)
            weekday = (
                profile_weekday(timestamp.date())
                if data.get("schemaVersion", 1) == 2
                else timestamp.weekday()
            )
            if source == 1:
                row = self.observed(fleet_id, route, timestamp)
                if row["vehicles"] is None:
                    row = self.profile(fleet_id, route, weekday, timestamp.hour, timestamp.isoformat())
            elif source == 2 and issued_at:
                row = self.profile(fleet_id, route, weekday, timestamp.hour, issued_at)
            else:
                row = missing()
            records.append(row)
        return records
