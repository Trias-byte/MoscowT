import shutil
from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_models import specs

from moscowt.api import create_app
from moscowt.bundles import create_bundle, restore_bundle
from moscowt.config import Settings
from moscowt.domain import DomainError, TimeRange
from moscowt.modeling.contracts import ForecastSpec
from moscowt.modeling.service import ModelService
from moscowt.platform_exports import PeriodExporter, PeriodExportSpec
from moscowt.platform_worker import execute_and_finish
from moscowt.scenarios import ScenarioService, ScenarioSpec
from moscowt.schedules import ScheduleService, integrate
from moscowt.tasks import WorkQueue


def test_platform_job_publish_scenario_export_restore(model_data, store, tmp_path):
    data, ident, local = model_data
    shutil.copytree(store.root / "networks", local.root / "networks")
    local.publish(networkId=store.current()["networkId"])
    settings = Settings(state_dir=local.root, data_root=data.root, worker_enabled=False)
    queue = WorkQueue(local.root)
    with TestClient(create_app(settings)) as client:
        body = specs(ident, "seasonal").model_dump(mode="json")
        response = client.post("/api/v2/training-runs", json=body, headers={"Idempotency-Key": "training"})
        assert response.status_code == 202, response.text
        job = queue.claim(("train",), "owner")
        execute_and_finish(settings, job)
        result = client.get(f"/api/v2/jobs/{job['id']}").json()
        assert result["status"] == "ready", result
        model_id = result["result"]["id"]
        spec = ForecastSpec(
            model_id=model_id,
            dataset_id=ident,
            route_ids=body["route_ids"],
            origin="2026-03-01T00:00:00+03:00",
            time_range={"start": "2026-03-01T00:00:00+03:00", "end": "2026-03-03T00:00:00+03:00"},
        )
        run = ModelService(data, local).predict(spec)
        published = client.post("/api/v2/publications", json={"forecast_id": run["id"]})
        assert published.status_code == 200, published.text
        assert client.get("/health/ready").status_code == 200
        caps = client.get("/api/v1/capabilities").json()
        assert set(caps["targetRouteIds"]) == {"5", "new-101"}
        assert client.get("/api/v1/network", params={"date": "2026-03-01"}).status_code == 200
        scenario = client.post(
            "/api/v2/scenarios",
            json={
                "forecast_id": run["id"],
                "route_ids": ["new-101"],
                "time_range": spec.time_range.model_dump(mode="json"),
                "coefficients": {"weather": 1.5},
            },
        ).json()
        query = {
            "dataset_id": ident,
            "forecast_id": run["id"],
            "route_ids": spec.route_ids,
            "time_range": spec.time_range.model_dump(mode="json"),
            "mode": "forecast",
            "scenario_id": scenario["id"],
        }
        rows = client.post("/api/v2/forecasts/query", json=query).json()["rows"]
        assert all(
            row["value"] == row["base_value"] * (1.5 if row["route"] == "new-101" else 1) for row in rows
        )
        exported = PeriodExporter(settings, data, ModelService(data, local)).write(
            PeriodExportSpec.model_validate(query), "test-export"
        )
        csv = pd.read_csv(local.root / "exports" / exported["filename"], sep=";")
        assert csv.value.tolist() == [row["value"] for row in rows]
        scope = {
            "snapshotId": caps["snapshotId"],
            "routeIds": spec.route_ids,
            "timeRange": spec.time_range.model_dump(mode="json"),
            "scenarioId": scenario["id"],
        }
        view = client.post("/api/v1/map-snapshot", json=scope)
        assert view.status_code == 200, view.text
        frame_values = {(r["route"], pd.Timestamp(r["timestamp"])): r["value"] for r in rows}
        for frame in view.json()["frames"]:
            for value in frame["values"]:
                key = (
                    value["routeId"],
                    pd.Timestamp(frame["start"]),
                )
                assert value["value"] == frame_values[key]
    result = create_bundle(settings, "test-bundle")
    restored = Settings(
        state_dir=tmp_path / "restored-state", data_root=tmp_path / "restored-data", worker_enabled=False
    )
    restore_bundle(local.root / "exports" / result["filename"], restored)
    with TestClient(create_app(restored)) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v2/models").json()[0]["id"] == model_id
        from moscowt.data.repository import DatasetRepository

        restored_data = DatasetRepository(restored.data_root)
        part = restored_data.manifest(ident)["partitions"][0]
        restored_data.files.path("partitions", part["id"], "parquet").unlink()
        assert client.get("/health/ready").status_code == 503


def test_lease_owner_cancel_and_expiry(tmp_path):
    queue = WorkQueue(tmp_path)
    job = queue.enqueue("train", {"x": 1}, "key")
    assert queue.enqueue("train", {"x": 1}, "key")["id"] == job["id"]
    claimed = queue.claim(("train",), "one")
    queue.finish(claimed["id"], "someone-else", result={})
    assert queue.get(job["id"])["status"] == "running"
    queue.cancel(job["id"])
    assert not queue.heartbeat(job["id"], "one")
    queue.finish(job["id"], "one", result={})
    assert queue.get(job["id"])["status"] == "cancelled"
    new = queue.enqueue("forecast", {}, "two")
    queue.claim(("forecast",), "first")
    with queue.connect() as db:
        db.execute("UPDATE work_items SET lease_until=0 WHERE id=?", (new["id"],))
    assert queue.claim(("forecast",), "second")["id"] == new["id"]
    assert queue.get(new["id"])["attempts"] == 2


def test_schedules_partial_hours_night_dedup_and_conflict(tmp_path):
    path = tmp_path / "duties.csv"
    path.write_text(
        "route;service_date;grafic;trip_num;direction;start;end;garage_number\n1;2026-09-01;0206;1;out;23:30;25:30;00042\n1;2026-09-01;206;2;out;24:00;25:00;42\n"
    )
    service = ScheduleService(tmp_path / "data")
    schedule = service.import_file(path)
    period = TimeRange(start="2026-09-01T23:00:00+03:00", end="2026-09-02T02:00:00+03:00")
    rows = service.hours(schedule["id"], period, ["1"])
    assert [row["vehicle_hours"] for row in rows] == [0.5, 1, 0.5]
    events = pd.DataFrame(
        {
            "route": ["1"],
            "event_time": [pd.Timestamp("2026-09-01T23:40:00+03:00")],
            "bus_exit_no": ["206"],
            "garage_number": ["42"],
        }
    )
    assert service.reconcile(schedule["id"], [events])["matched_events"] == 1
    same = datetime.fromisoformat("2026-09-01T23:30:00+03:00")
    _, conflicts = integrate(
        [
            {"route": "1", "resource": "42", "start": same, "end": period.end},
            {"route": "2", "resource": "42", "start": same, "end": period.end},
        ],
        period,
    )
    assert conflicts == {"1", "2"}


def test_scenario_neutral_and_slice_budget(model_data):
    data, ident, store = model_data
    service = ModelService(data, store)
    model = service.train(specs(ident, "seasonal"))
    spec = ForecastSpec(
        model_id=model["id"],
        dataset_id=ident,
        route_ids=["5"],
        origin="2026-03-01T00:00:00+03:00",
        time_range={"start": "2026-03-01T00:00:00+03:00", "end": "2026-03-02T00:00:00+03:00"},
    )
    run = service.predict(spec)
    _, frame = service.frame(run["id"])
    frame["vehicle_hours"] = 1.0
    scenarios = ScenarioService(store)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    scenario_spec = ScenarioSpec(
        forecast_id=run["id"], route_ids=["5"], time_range=spec.time_range, additional_vehicle_hours=24
    )
    barrier = Barrier(8)

    def create_once(_):
        barrier.wait()
        return scenarios.create(scenario_spec)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create_once, range(8)))
    scenario = results[0]
    assert all(result == scenario for result in results)
    full = scenarios.apply(frame, scenario["id"], run["id"])
    part = scenarios.apply(frame.iloc[:2], scenario["id"], run["id"])
    assert full.value.equals(frame.value)
    assert full.vehicle_hours.sum() == 48
    assert part.vehicle_hours.tolist() == [2, 2]
    with pytest.raises(DomainError):
        scenarios.apply(frame, scenario["id"], "other")


def test_monthly_aggregation_and_unsupported_horizon(model_data):
    from moscowt.modeling.contracts import TrainingSpec

    data, ident, store = model_data
    service = ModelService(data, store)
    training = specs(ident, "annual_scenario")
    model = service.train(training)
    spec = ForecastSpec(
        model_id=model["id"],
        dataset_id=ident,
        route_ids=training.route_ids,
        origin="2026-03-01T00:00:00+03:00",
        time_range={"start": "2026-03-01T00:00:00+03:00", "end": "2027-03-01T00:00:00+03:00"},
    )
    run = service.predict(spec)
    export = PeriodExportSpec(
        dataset_id=ident,
        forecast_id=run["id"],
        route_ids=spec.route_ids,
        time_range=spec.time_range,
        mode="forecast",
    )
    exporter = PeriodExporter(Settings(state_dir=store.root, data_root=data.root), data, service)
    hourly = exporter.frame(export)
    monthly = exporter.frame(export.model_copy(update={"grain": "month"}))
    assert len(monthly) == 12 * len(spec.route_ids)
    assert monthly.value.sum() == pytest.approx(hourly.value.sum())
    assert "Невалидированный" in run["quality_note"]
    seasonal = service.train(TrainingSpec(**{**training.model_dump(), "model_type": "seasonal"}))
    with pytest.raises(DomainError, match="годовой"):
        service.predict(spec.model_copy(update={"model_id": seasonal["id"]}))


def test_bundle_remains_consistent_during_publication(model_data, tmp_path, monkeypatch):
    import tarfile

    from moscowt.storage import SnapshotStore

    data, ident, store = model_data
    expected = store.publish(datasetId=ident)
    settings = Settings(state_dir=store.root, data_root=data.root, worker_enabled=False)
    original_add = tarfile.TarFile.add
    changed = False

    def publish_while_archiving(archive, *args, **kwargs):
        nonlocal changed
        if not changed:
            store.publish(scheduleScenario=True)
            changed = True
        return original_add(archive, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", publish_while_archiving)
    bundle = create_bundle(settings, "concurrent-publication")
    restored = Settings(state_dir=tmp_path / "restored-state", data_root=tmp_path / "restored-data")
    restore_bundle(store.root / "exports" / bundle["filename"], restored)
    assert changed and store.current() != expected
    assert SnapshotStore(restored.state_dir).current() == expected
