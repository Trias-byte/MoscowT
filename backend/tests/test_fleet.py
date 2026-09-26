import csv
import io
import runpy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from moscowt.domain import TZ, DomainError, ExportRequest
from moscowt.exports import user_csv
from moscowt.fleet import FleetService, fleet_summary, missing
from moscowt.pipelines import prepare_labels
from moscowt.storage import SnapshotStore


def test_raw_scan_counts_vehicles_not_validators_across_chunks_and_files(tmp_path):
    scan = runpy.run_path(str(Path(__file__).parents[1] / "scripts/build_fleet_observations.py"))["scan"]
    header = "tran_date_time;ngpt_route;garage_number;device_no;validation_result\n"
    first, second = tmp_path / "train.csv", tmp_path / "test.csv"
    first.write_text(
        header
        + "\n".join(
            [
                "2025-01-01 08:00:00;1 трамвай;00123;validator1;success",
                "2025-01-01 08:30:00;1 трамвай;123;validator2;success",
                "2025-01-01 08:45:00;1 трамвай;456;validator3;failed",
                "2025-01-01 08:45:00;1 трамвай;;validator4;success",
                "2025-01-01 08:45:00;1 трамвай;0;validator5;success",
                "2025-11-01 00:00:00;1 трамвай;789;validator6;success",
            ]
        )
    )
    second.write_text(
        header
        + "\n".join(
            [
                "2025-01-01 08:50:00;1 трамвай; 123 ;validator7;success",
                "2025-01-01 09:00:00;1 трамвай;123;validator1;success",
                "2025-01-01 08:59:00;7 трамвай;123;validator1;success",
            ]
        )
    )
    result = scan([first, second], chunk_size=1)
    series = result["routes"]["1"]
    assert series["vehicles"][8:10] == [2, 1]
    assert series["events"][8] == 6 and series["identifiedEvents"][8] == 4
    assert result["routes"]["7"]["vehicles"][8] == 1
    assert result["inputs"][0]["outsideHistory"] == 1
    assert result["inputs"][0]["missingVehicleEvents"] == 2
    assert len(series["vehicles"]) == 7296
    assert len(result["inputs"][0]["sha256"]) == 64


@pytest.fixture
def synthetic_fleet(tmp_path):
    store = SnapshotStore(tmp_path)
    start = datetime(2025, 1, 1, tzinfo=TZ)
    size = 24 * 180
    series = {key: [0] * size for key in ("vehicles", "events", "identifiedEvents")}
    # Every day has a radically different 09:00 count from its 08:00 count.
    for day in range(180):
        for hour, count in ((8, 4 if (start + timedelta(days=day)).weekday() == 5 else 12), (9, 40)):
            i = day * 24 + hour
            series["vehicles"][i] = count
            series["events"][i] = count * 2
            series["identifiedEvents"][i] = count * 2
    # Evidence at/after the issue must not enter its estimated fleet profile.
    issue = datetime(2025, 3, 1, 8, tzinfo=TZ)
    i = int((issue - start).total_seconds() / 3600)
    for j in range(i, size):
        if j % 24 == 8:
            series["vehicles"][j] = 999
            series["events"][j] = series["identifiedEvents"][j] = 999
    store.put(
        "fleets",
        "fleet-test",
        {
            "historyId": "history-test",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=size)).isoformat(),
            "routes": {"1": series},
        },
    )
    return FleetService(store), {"fleetId": "fleet-test", "historyId": "history-test"}, issue


def test_profile_uses_same_weekday_hour_before_issue_and_requires_support(synthetic_fleet):
    service, snapshot, issue = synthetic_fleet
    future = datetime(2025, 3, 8, 8, tzinfo=TZ)
    records = service.hours(snapshot, future, "1", [2], issue.isoformat())
    assert records == [{"vehicles": 4, "source": "estimated", "sampleDays": 8, "coverage": 1.0}]
    # A partially completed hour cannot leak events from the remainder of that hour.
    assert (
        service.profile("fleet-test", "1", issue.weekday(), 8, issue.replace(minute=30).isoformat())[
            "vehicles"
        ]
        == 4
    )
    # Two same-weekday samples are insufficient; unrelated hours and days do not help.
    assert service.profile("fleet-test", "1", 2, 8, "2025-01-15T08:00:00+03:00") == missing()
    assert service.profile("fleet-test", "5", 5, 8, issue.isoformat()) == missing()
    night = service.profile("fleet-test", "1", 5, 3, issue.isoformat())
    assert night["vehicles"] == 0 and night["source"] == "estimated"
    # Full historical evidence remains available when displaying actual observations.
    assert service.hours(snapshot, future, "1", [1], issue.isoformat())[0]["vehicles"] == 999
    with pytest.raises(DomainError, match="другой истории"):
        service.hours({**snapshot, "historyId": "history-other"}, future, "1", [1], None)


def test_fleet_summary_is_weighted_and_unknown_is_never_one_vehicle():
    def record(count, source="observed"):
        return {"vehicles": count, "source": source, "sampleDays": 8, "coverage": 0.99}

    result = fleet_summary([record(2), record(10, "estimated")], 720)
    assert result["fleetVehicles"] == 6 and result["vehicleHours"] == 12
    assert result["loadPerVehicleHour"] == 60 and result["fleetSource"] == "mixed"
    assert fleet_summary([record(2), missing()], 720)["loadPerVehicleHour"] is None
    assert fleet_summary([record(0)], 0)["loadPerVehicleHour"] is None
    assert fleet_summary([record(2)], 0)["loadPerVehicleHour"] == 0
    assert fleet_summary([record(2)], None)["loadPerVehicleHour"] is None


def test_reusing_a_history_resets_fleet_only_when_history_changes(store, dataset, tmp_path):
    isolated = SnapshotStore(tmp_path)
    history = store.current()["historyId"]
    isolated.put("histories", history, store.read("histories", history))
    isolated.publish(historyId="history-other", fleetId="fleet-old")
    prepare_labels(isolated, dataset)
    assert isolated.current()["fleetId"] is None
    isolated.publish(fleetId="fleet-current")
    prepare_labels(isolated, dataset)
    assert isolated.current()["fleetId"] == "fleet-current"


def test_api_vehicle_counts_match_raw_day_and_forecast_export(client, store, scope):
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["fleetId"] == store.current()["fleetId"]
    assert capabilities["vehicleLoadThresholds"] == [5, 20, 40, 70]
    scope.update(
        mode="auto",
        routeIds=["17"],
        timeRange={
            "start": "2025-10-31T00:00:00+03:00",
            "end": "2025-11-01T00:00:00+03:00",
        },
    )
    hourly = client.post("/api/v1/map-snapshot", json=scope).json()
    daily = client.post("/api/v1/map-snapshot", json={**scope, "grain": "day"}).json()["frames"][0]["values"][
        0
    ]
    rows = [f["values"][0] for f in hourly["frames"]]
    raw = store.read("fleets", capabilities["fleetId"])
    offset = int(
        (
            datetime.fromisoformat(scope["timeRange"]["start"]) - datetime.fromisoformat(raw["start"])
        ).total_seconds()
        / 3600
    )
    assert [v["fleetVehicles"] for v in rows] == raw["routes"]["17"]["vehicles"][offset : offset + 24]
    assert all(v["fleetSource"] == "observed" for v in rows)
    assert daily["vehicleHours"] == sum(v["vehicleHours"] for v in rows)
    assert daily["fleetVehicles"] == daily["vehicleHours"] / 24
    assert daily["loadPerVehicleHour"] == daily["value"] / daily["vehicleHours"]
    scope["timeRange"] = {"start": "2025-11-01T08:00:00+03:00", "end": "2025-11-01T09:00:00+03:00"}
    forecast = client.post("/api/v1/map-snapshot", json=scope).json()
    row = forecast["frames"][0]["values"][0]
    assert row["fleetSource"] == "estimated" and row["fleetSampleDays"] == 8
    assert row["fleetVehicles"] > 0 and row["loadPerVehicleHour"] > 0
    exported = next(
        csv.DictReader(
            io.StringIO(user_csv(store, ExportRequest(scope=scope)).decode("utf-8-sig")), delimiter=";"
        )
    )
    assert exported["fleet_id"] == capabilities["fleetId"]
    assert exported["fleet_source"] == "estimated"
    assert float(exported["validations_per_vehicle_hour"]) == row["loadPerVehicleHour"]
    scope["routeIds"] = ["5"]
    no_fleet = client.post("/api/v1/map-snapshot", json=scope).json()["frames"][0]["values"][0]
    assert no_fleet["value"] == 0
    assert no_fleet["fleetSource"] == "missing" and no_fleet["loadPerVehicleHour"] is None
