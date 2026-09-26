"""Reproducible daily 2025 service evidence + activity estimates; no invented trips."""

import argparse
import csv
import gzip
import hashlib
import html
import json
import math
import re
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from moscowt.domain import TZ
from moscowt.fleet import FleetService
from moscowt.network_history import resolve_network
from moscowt.service_calendar import CALENDAR_SOURCE, is_workday, profile_weekday
from moscowt.storage import SnapshotStore


def archived_departures(text):
    """Parse dated public stop timetables, preserving direction and duplicate times.

    A clock time belongs to a stop. There is no reliable trip key in this HTML.
    Do not zip equal-index times at different stops into invented trips.
    """
    rows = []
    blocks = re.findall(r'<li\b[^>]*data-route="141501932"[^>]*>.*?</li>', text, re.S)
    for order, block in enumerate(blocks, 1):
        attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', block.split(">", 1)[0]))
        name = re.search(r'<div class="a_dotted d-inline">(.*?)</div>', block, re.S)
        if not name or attrs.get("data-date") != "2025-07-29" or attrs.get("data-direction") != "0":
            raise ValueError("Unexpected archived date/direction/stop")
        hours = list(re.finditer(r"<strong>(\d\d):</strong>", block))
        for i, hour_match in enumerate(hours):
            hour = int(hour_match.group(1))
            section = block[hour_match.end() : hours[i + 1].start() if i + 1 < len(hours) else len(block)]
            for minute in re.findall(r'<div class="div10"\s*>\s*(\d\d)\s*</div>', section):
                if not 0 <= hour < 24 or not 0 <= int(minute) < 60:
                    raise ValueError("Invalid departure clock time")
                rows.append(
                    {
                        "schedule_date": attrs["data-date"],
                        "route_id": "7",
                        "direction": 0,
                        "stop_order": order,
                        "source_stop_key": attrs["data-stop"],
                        "stop_name": html.unescape(name.group(1)),
                        "time_msk": f"{hour:02d}:{minute}",
                        "source_id": "official-route7-2025-07-29",
                    }
                )
    if not rows or len(blocks) < 2:
        raise ValueError("No stop timetable in archived page")
    return rows


def path_km(points):
    result = 0
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        a, b = math.radians(lat1), math.radians(lat2)
        value = (
            math.sin((b - a) / 2) ** 2
            + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
        )
        result += 6371 * 2 * math.asin(min(1, math.sqrt(value)))
    return result


def reference_fleet(km, low_interval, high_interval, assumptions):
    """Both directions' total km / km per hour / departure interval in hours."""
    if km is None or low_interval is None:
        return None, None, None
    return (
        km * 60 / assumptions["referenceSpeedMaxKmh"] / high_interval,
        km * 60 / assumptions["referenceSpeedKmh"] / ((low_interval + high_interval) / 2),
        km * 60 / assumptions["referenceSpeedMinKmh"] / low_interval,
    )


def notices(day, route):
    # These are alerts to review, never a made-up end date or automatic zero.
    flags, sources = [], []
    if route in ("7", "50") and day >= "2025-07-10":
        flags.append(
            "announced_temporary_variant_unverified_geometry"
            if day < "2025-08-07"
            else "service_restoration_date_not_verified"
        )
        sources.append("routes7-50-july-change")
    if route == "17" and day >= "2025-04-05" and date.fromisoformat(day).weekday() >= 5:
        flags.append("weekend_change_notice_end_not_verified")
        sources.append("route17-april-suspension" if day >= "2025-04-26" else "route17-april-change")
    return flags, sources


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build(dataset, state):
    folder = dataset / "derived/schedules-2025"
    manifest = json.loads((folder / "sources.json").read_bytes())
    for source in manifest["sources"]:
        if source.get("localFile"):
            content = (folder / source["localFile"]).read_bytes()
            if source["localFile"].endswith(".gz"):
                content = gzip.decompress(content)
            checksum = hashlib.sha256(content).hexdigest()
            if source.get("sha256") and source["sha256"] != checksum:
                raise ValueError(f"Pinned source changed: {source['id']}")
            source["sha256"] = checksum
    departures = archived_departures(
        gzip.decompress((folder / "sources/route7-2025-07-29.html.gz").read_bytes()).decode()
    )
    raw = json.loads((dataset / "derived/fleet/observed-activity.json").read_bytes())
    original = json.loads((dataset / "derived/fleet/observed-fleet.json").read_bytes())
    # The diagnostics must reproduce the previous scan before replacing it.
    assert raw["inputs"] == original["inputs"]
    for route in manifest["scope"]:
        assert raw["routes"][route]["hourlyDistinctVehicles"] == original["routes"][route]["vehicles"]
        for key in ("events", "identifiedEvents"):
            assert raw["routes"][route][key] == original["routes"][route][key]
    store = SnapshotStore(state)
    current = store.current()
    archive = store.read("networks", current["networkId"])
    # Use the dated OSM source, not arbitrary user CSV overrides, for the audit.
    if archive.get("baseArchiveId"):
        archive = store.read("networks", archive["baseArchiveId"])
    intervals = {r["routeId"]: r for r in manifest["intervalReferences"]}
    start = datetime(2025, 1, 1, tzinfo=TZ)
    cutoff = datetime(2025, 11, 1, tzinfo=TZ)
    hourly, daily = [], []
    with tempfile.TemporaryDirectory(prefix="moscowt-fleet-dataset-") as temp:
        temporary = SnapshotStore(Path(temp))
        temporary.put("fleets", "fleet-research", {**raw, "historyId": "history-research"})
        fleet = FleetService(temporary)
        snapshot = {"fleetId": "fleet-research", "historyId": "history-research"}
        for day_index in range(365):
            timestamp = start + timedelta(days=day_index)
            day = timestamp.date().isoformat()
            network = resolve_network(store, archive, day)
            for route in manifest["scope"]:
                not_started = route == "5" and day < "2025-12-16"
                records = fleet.hours(
                    snapshot, timestamp, route, [1 if timestamp < cutoff else 2] * 24, cutoff.isoformat()
                )
                series = raw["routes"][route]
                offset = day_index * 24
                for hour, record in enumerate(records):
                    idx = offset + hour
                    hourly.append(
                        {
                            "timestamp_msk": (timestamp + timedelta(hours=hour)).isoformat(),
                            "route_id": route,
                            "mean_active_vehicles_estimate": record["vehicles"],
                            "fleet_source": record["source"],
                            "sample_days": record["sampleDays"],
                            "identifier_coverage": record["coverage"],
                            "direct_five_minute_activity": series["directActivityVehicles"][idx]
                            if timestamp < cutoff
                            else None,
                            "old_hourly_distinct_vehicles": series["hourlyDistinctVehicles"][idx]
                            if timestamp < cutoff
                            else None,
                            "forecast_issued_at": cutoff.isoformat() if timestamp >= cutoff else None,
                            "profile_weekday_proxy": profile_weekday(timestamp.date()),
                        }
                    )
                counts = [r["vehicles"] for r in records]
                complete = all(n is not None for n in counts)
                interval = intervals[route]
                if not_started:
                    low = high = None
                    ref_source = "route5-opening"
                else:
                    low, high = interval["minutesMin"], interval["minutesMax"]
                    ref_source = (
                        "official-map-2025-12-15" if day >= "2025-12-16" else "official-map-2024-10-09"
                    )
                flags, alerts = notices(day, route)
                patterns = [p for p in network["patterns"] if p["routeId"] == route]
                distance = (
                    sum(path_km(s["coordinates"]) for s in network["segments"] if s["routeId"] == route)
                    if len(patterns) == 2
                    else None
                )
                # A regular-route geometric proxy cannot describe a known variant.
                estimates = reference_fleet(
                    distance if not flags else None, low, high, manifest["assumptions"]
                )
                partial_timetable = route == "7" and day == "2025-07-29"
                sources = [ref_source, *alerts]
                if partial_timetable:
                    sources.append("official-route7-2025-07-29")
                first_stop = [r for r in departures if r["stop_order"] == 1] if partial_timetable else []
                day_slice = slice(offset, offset + 24)
                daily.append(
                    {
                        "date": day,
                        "route_id": route,
                        "weekday": timestamp.weekday(),
                        "civil_day_type": "workday" if is_workday(timestamp.date()) else "nonworkday",
                        "transport_day_type_confirmed": False,
                        "service_status": "not_started" if not_started else "not_independently_verified",
                        "timetable_coverage": "one_direction_one_date" if partial_timetable else "not_found",
                        "published_departures_first_stop_one_direction": len(first_stop)
                        if first_stop
                        else None,
                        "mean_interval_reference_min": low,
                        "mean_interval_reference_max": high,
                        "interval_applies_to_date_confirmed": False,
                        "interval_source_id": ref_source,
                        "osm_source_date": network.get("sourceAsOf", network["asOf"]),
                        "round_trip_distance_km_reference": round(distance, 4) if distance else None,
                        "fleet_reference_low": round(estimates[0], 4) if estimates[0] is not None else None,
                        "fleet_reference_mid": round(estimates[1], 4) if estimates[1] is not None else None,
                        "fleet_reference_high": round(estimates[2], 4) if estimates[2] is not None else None,
                        "fleet_mean_24h_activity_estimate": sum(counts) / 24 if complete else None,
                        "vehicle_hours_activity_estimate": sum(counts) if complete else None,
                        "fleet_peak_hour_activity_estimate": max(counts) if complete else None,
                        "old_mean_hourly_distinct": sum(series["hourlyDistinctVehicles"][day_slice]) / 24
                        if timestamp < cutoff
                        else None,
                        "direct_mean_5min_activity": sum(series["directActivityVehicles"][day_slice]) / 24
                        if timestamp < cutoff
                        else None,
                        "activity_source": "validation_activity_estimate"
                        if timestamp < cutoff
                        else "historical_profile_estimate",
                        "quality_flags": "|".join(
                            ["incomplete_timetable_archive", "reference_is_not_daily_dispatch", *flags]
                        ),
                        "source_ids": "|".join(sources),
                        "calendar_source_url": CALENDAR_SOURCE,
                    }
                )
    assert len(daily) == 3650 and len(hourly) == 87600
    assert len({(r["date"], r["route_id"]) for r in daily}) == len(daily)
    assert len({(r["timestamp_msk"], r["route_id"]) for r in hourly}) == len(hourly)
    folder.mkdir(parents=True, exist_ok=True)
    write_csv(folder / "daily_service_2025.csv", daily)
    write_csv(folder / "hourly_fleet_2025.csv", hourly)
    write_csv(folder / "archived_stop_departures.csv", departures)
    summary = {
        "networkArchiveId": archive.get("id", archive.get("networkSnapshotId")),
        "activityArtifactSha256": hashlib.sha256(
            (dataset / "derived/fleet/observed-activity.json").read_bytes()
        ).hexdigest(),
        "activityForecastIssuedAt": cutoff.isoformat(),
        "fleetMethod": raw["method"],
        "dailyRows": len(daily),
        "hourlyRows": len(hourly),
        "archivedStopDepartures": len(departures),
        "archiveStops": len({r["stop_order"] for r in departures}),
        "archiveFirstStopDepartures": sum(r["stop_order"] == 1 for r in departures),
        "completeTimetableRouteDays": 0,
        "partialTimetableRouteDays": 1,
        "calendarWorkdays": sum(is_workday((start + timedelta(days=i)).date()) for i in range(365)),
        "timetableCoverage": dict(Counter(r["timetable_coverage"] for r in daily)),
        "crossRouteVehicleBins": raw["crossRouteVehicleBins"],
        "byRoute": {
            route: {
                "oldVehicleHoursJanOct": sum(raw["routes"][route]["hourlyDistinctVehicles"]),
                "activityVehicleHoursJanOct": sum(raw["routes"][route]["vehicles"]),
                "directVehicleHoursJanOct": sum(raw["routes"][route]["directActivityVehicles"]),
            }
            for route in manifest["scope"]
        },
        "artifactHashes": {
            name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in ("daily_service_2025.csv", "hourly_fleet_2025.csv", "archived_stop_departures.csv")
        },
    }
    (folder / "sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("byRoute", "artifactHashes")},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("dataset"))
    parser.add_argument("--state-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    build(args.dataset_dir, args.state_dir)
