import gzip
import json
import runpy
from datetime import date, timedelta
from pathlib import Path

import pytest

from moscowt.fleet import FleetService
from moscowt.service_calendar import is_workday, profile_weekday
from moscowt.storage import SnapshotStore

ROOT = Path(__file__).parents[2]
ACTIVITY = runpy.run_path(str(ROOT / "backend/scripts/build_fleet_activity.py"))
DAILY = runpy.run_path(str(ROOT / "backend/scripts/build_daily_service.py"))


def test_activity_averages_time_and_deduplicates_files_and_validators(tmp_path):
    header = "tran_date_time;ngpt_route;garage_number\n"
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text(
        header
        + "\n".join(
            [
                "2025-01-01 08:00:00;17 трамвай;0123",
                "2025-01-01 08:15:00;17 трамвай;123",
                "2025-01-01 08:55:00;17 трамвай;456",
                "2025-01-01 08:55:00;17 трамвай;",
            ]
        )
    )
    b.write_text(header + "2025-01-01 08:00:01;17 трамвай;123\n")
    result = ACTIVITY["scan"]([a, b], chunk_size=1)
    series = result["routes"]["17"]
    assert series["hourlyDistinctVehicles"][8] == 2
    assert series["directActivityVehicles"][8] == pytest.approx(3 / 12)
    assert series["vehicles"][8] == pytest.approx(5 / 12)
    assert series["events"][8] == 5
    assert series["identifiedEvents"][8] == 4
    assert not series["vehicles"][9]  # An observation does not fill the next hour.


def test_short_gaps_do_not_cross_other_routes_or_long_silent_periods():
    fill = ACTIVITY["fill_short_gaps"]
    assert fill(0b10001) == 0b11111
    assert fill(0b100001) == 0b100001
    assert fill(0b10001, 0b00100) == 0b10001


def test_civil_calendar_and_proxy_are_distinct_from_transport_confirmation():
    start = date(2025, 1, 1)
    assert sum(is_workday(start + timedelta(days=i)) for i in range(365)) == 247
    assert is_workday(date(2025, 11, 1))
    assert profile_weekday(date(2025, 11, 1)) == 4
    assert not is_workday(date(2025, 11, 3))
    assert profile_weekday(date(2025, 11, 3)) == 6
    assert profile_weekday(date(2025, 10, 25)) == 5


def test_activity_profile_keeps_fractional_vehicle_hours_and_holiday_proxy(tmp_path):
    from datetime import datetime

    from moscowt.domain import TZ

    start = datetime(2025, 8, 1, tzinfo=TZ)
    end = datetime(2025, 11, 1, tzinfo=TZ)
    values = [2.5 if (start + timedelta(hours=i)).weekday() == 4 else 0.5 for i in range(92 * 24)]
    store = SnapshotStore(tmp_path)
    store.put(
        "fleets",
        "fleet-test",
        {
            "schemaVersion": 2,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "historyId": "h",
            "routes": {
                "17": {
                    "vehicles": values,
                    "events": [100] * len(values),
                    "identifiedEvents": [100] * len(values),
                }
            },
        },
    )
    service = FleetService(store)
    result = service.hours({"fleetId": "fleet-test", "historyId": "h"}, end, "17", [2], end.isoformat())[0]
    assert result["vehicles"] == 2.5  # Friday, not Saturday; never round to three.
    assert result["sampleDays"] == 8


def test_profile_does_not_use_gap_fills_confirmed_after_issue(tmp_path):
    from datetime import datetime

    from moscowt.domain import TZ

    store = SnapshotStore(tmp_path)
    # Rising Friday observations make inclusion of the final, unavailable Friday visible.
    start = datetime(2025, 8, 1, tzinfo=TZ)
    values = [float(i // (7 * 24) + 1) for i in range(92 * 24)]
    store.put(
        "fleets",
        "fleet-test",
        {
            "schemaVersion": 2,
            "start": start.isoformat(),
            "end": "2025-11-01T00:00:00+03:00",
            "routes": {
                "17": {
                    "vehicles": values,
                    "events": [100] * len(values),
                    "identifiedEvents": [100] * len(values),
                }
            },
        },
    )
    service = FleetService(store)
    before = service.profile("fleet-test", "17", 4, 8, "2025-10-31T09:00:00+03:00")
    after = service.profile("fleet-test", "17", 4, 8, "2025-10-31T09:20:00+03:00")
    assert before["vehicles"] == 9.5
    assert after["vehicles"] == 10.5


def test_archive_preserves_stop_times_without_inventing_trip_ids():
    path = ROOT / "dataset/derived/schedules-2025/sources/route7-2025-07-29.html.gz"
    rows = DAILY["archived_departures"](gzip.decompress(path.read_bytes()).decode())
    assert {r["schedule_date"] for r in rows} == {"2025-07-29"}
    assert {r["direction"] for r in rows} == {0}
    assert {r["route_id"] for r in rows} == {"7"}
    assert len({r["stop_order"] for r in rows}) > 10
    assert all("trip_id" not in r for r in rows)
    assert all(0 <= int(r["time_msk"][:2]) < 24 for r in rows)
    with pytest.raises(ValueError):
        DAILY["archived_departures"]("<p>Расписание на заданную дату не найдено</p>")


def test_reference_units_and_unconfirmed_change_end():
    assumptions = {"referenceSpeedMinKmh": 14, "referenceSpeedKmh": 16, "referenceSpeedMaxKmh": 20}
    low, mid, high = DAILY["reference_fleet"](32, 6, 8, assumptions)
    assert mid == pytest.approx(120 / 7)
    assert low < mid < high
    assert DAILY["reference_fleet"](None, 6, 8, assumptions) == (None, None, None)
    flags, _ = DAILY["notices"]("2025-05-03", "17")
    assert "weekend_change_notice_end_not_verified" in flags
    flags, _ = DAILY["notices"]("2025-08-07", "7")
    assert "service_restoration_date_not_verified" in flags


def test_daily_dataset_is_complete_grid_with_honest_coverage():
    import csv

    folder = ROOT / "dataset/derived/schedules-2025"
    with (folder / "daily_service_2025.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3650
    assert len({(r["date"], r["route_id"]) for r in rows}) == 3650
    assert sum(r["timetable_coverage"] == "one_direction_one_date" for r in rows) == 1
    assert all(r["interval_applies_to_date_confirmed"] == "False" for r in rows)
    assert all(
        r["mean_interval_reference_min"] == ""
        for r in rows
        if r["route_id"] == "5" and r["date"] < "2025-12-16"
    )
    july = next(r for r in rows if r["route_id"] == "7" and r["date"] == "2025-07-29")
    assert int(july["published_departures_first_stop_one_direction"]) > 50
    assert not july["fleet_reference_mid"]  # Stale OSM full route cannot model the shortened service.
    manifest = json.loads((folder / "sources.json").read_bytes())
    assert "not_complete" in manifest["coverage"]


def test_dataset_downloads_preserve_provenance_and_do_not_expose_raw_data(client, store):
    response = client.get("/api/v1/service-dataset-2025/daily_service_2025.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "interval_applies_to_date_confirmed" in response.text
    assert client.get("/api/v1/service-dataset-2025/train.csv").status_code == 404
    assert (
        client.get("/api/v1/service-dataset-2025/sources.json")
        .json()["coverage"]
        .endswith("not_complete_2025_timetables")
    )
    quality = client.get("/api/v1/data-quality").json()
    fleet = store.read("fleets", store.current()["fleetId"])
    assert quality["fleetMethod"] == fleet["method"]
