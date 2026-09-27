"""Regression coverage for the remaining D09/D14–D18 findings in the supplied audit."""

import json
import shutil

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_scenario_engine import base_run

from moscowt.api import create_app
from moscowt.config import Settings
from moscowt.domain import DomainError
from moscowt.factor_context import describe_series
from moscowt.platform_api import PublishRequest
from moscowt.platform_exports import PeriodExporter, PeriodExportSpec
from moscowt.publication import publish_forecast
from moscowt.scenarios import ScenarioSpec
from moscowt.schedules import ScheduleService, schedule_table
from moscowt.storage import file_hash
from moscowt.tasks import MODEL_KINDS, WorkQueue


@pytest.fixture
def report_app(model_data, store):
    data, _, local = model_data
    shutil.copytree(store.root / "networks", local.root / "networks")
    local.publish(networkId=store.current()["networkId"])
    models, spec, run = base_run(model_data)
    settings = Settings(state_dir=local.root, data_root=data.root, worker_enabled=False)
    snapshot = publish_forecast(settings, data, models, PublishRequest(forecast_id=run["id"]))
    with TestClient(create_app(settings)) as client:
        yield client, models, spec, run, snapshot, settings


@pytest.mark.parametrize("corruption", ["missing", "bytes", "same_size"])
def test_d14_missing_or_corrupt_schedule_after_warm_cache(report_app, tmp_path, corruption):
    client, models, spec, run, snapshot, settings = report_app
    source = tmp_path / "schedule.csv"
    source.write_text(
        "route;service_date;grafic;trip_num;direction;start;end;garage_number\n5;2026-03-01;1;1;out;08:00;10:00;42\n"
    )
    schedules = ScheduleService(settings.data_root)
    schedule = schedules.import_file(source)
    body = {
        "route_ids": ["5"],
        "time_range": spec.time_range.model_dump(mode="json"),
        "schedule_id": schedule["id"],
    }
    url = f"/api/v2/forecast-runs/{run['id']}/factor-context"
    assert client.post(url, json=body).status_code == 200
    path = schedules.store.path("schedules", schedule["id"], "parquet")
    if corruption == "missing":
        path.unlink()
    elif corruption == "bytes":
        path.write_bytes(b"invalid parquet")
    else:
        content = path.read_bytes()
        path.write_bytes(b"FAIL" + content[4:])
    response = client.post(url, json=body)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "SCHEDULE_CORRUPTED"
    with pytest.raises(DomainError, match="повреждён"):
        schedule_table(settings.data_root, schedule["id"])


def test_d15_hint_scope_and_object_identity_are_validated(report_app):
    client, models, spec, run, snapshot, _ = report_app
    # The shipped archive ends in 2025; this fixture forecasts March 2026.
    # Supply explicit geometry for that period to exercise real object resolution.
    models.store.put(
        "networks",
        "network-factor-test",
        {
            "stops": [{"id": "stop-5", "routeId": "5"}, {"id": "stop-other", "routeId": "new-101"}],
            "segments": [{"id": "segment-5", "routeId": "5"}],
        },
    )
    models.store.put("snapshots", "snapshot-factor-test", {"networkId": "network-factor-test"})
    url = f"/api/v2/forecast-runs/{run['id']}/factor-context"
    body = {
        "route_ids": ["5"],
        "time_range": spec.time_range.model_dump(mode="json"),
        "snapshot_id": "snapshot-factor-test",
    }
    valid = client.post(url, json=body)
    assert valid.status_code == 200, valid.text
    assert valid.json()["route_ids"] == ["5"] and valid.json()["resolution"] == "route"
    assert valid.json()["weather"]["fields"] == {}  # Seasonal model: no invented weather.
    for obj in [{"kind": "stop", "id": "stop-5"}, {"kind": "segment", "id": "segment-5"}]:
        assert client.post(url, json={**body, "object": obj}).status_code == 200
    for obj in [{"kind": "stop", "id": "stop-other"}, {"kind": "route", "id": "new-101"}]:
        response = client.post(url, json={**body, "object": obj})
        assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_FACTOR_SCOPE"
    unknown = client.post(url, json={**body, "object": {"kind": "segment", "id": "unknown"}})
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "INVALID_FACTOR_OBJECT"
    outside = client.post(url, json={**body, "route_ids": ["outside"]})
    assert outside.status_code == 422
    future = client.post(
        url,
        json={
            **body,
            "time_range": {"start": "2026-03-02T00:00:00+03:00", "end": "2026-03-03T00:00:00+03:00"},
        },
    )
    assert future.status_code == 422


def test_d16_d17_research_opt_in_local_view_and_export_keep_publication(report_app, model_data):
    client, models, _, _, snapshot, settings = report_app
    _, _, research = base_run(model_data, purpose="research")
    model_id = research["spec"]["model_id"]
    assert research["id"] not in {f["id"] for f in client.get("/api/v2/forecast-runs").json()}
    assert model_id not in {m["id"] for m in client.get("/api/v2/models").json()}
    assert research["id"] in {
        f["id"] for f in client.get("/api/v2/forecast-runs?include_research=true").json()
    }
    assert model_id in {m["id"] for m in client.get("/api/v2/models?include_research=true").json()}
    before = file_hash(models.store.root / "current.json")
    published = client.post("/api/v2/publications", json={"forecast_id": research["id"]})
    assert published.status_code == 409 and published.json()["error"]["code"] == "RESEARCH_ONLY"
    competition = client.post(
        "/api/v2/competition-exports",
        json={"forecast_id": research["id"]},
        headers={"Idempotency-Key": "research"},
    )
    assert competition.status_code == 409 and competition.json()["error"]["code"] == "RESEARCH_ONLY"
    mapped = client.post(
        "/api/v2/forecast-map-views",
        json={"forecast_id": research["id"], "snapshot_id": snapshot["snapshotId"]},
    )
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["forecastId"] == research["id"]
    assert file_hash(models.store.root / "current.json") == before
    assert client.get(f"/api/v2/forecast-runs/{research['id']}").json()["purpose"] == "research"
    request = PeriodExportSpec(
        dataset_id=research["spec"]["dataset_id"],
        forecast_id=research["id"],
        route_ids=["5"],
        time_range=research["spec"]["time_range"],
        mode="forecast",
        format="submission",
    )
    exporter = PeriodExporter(settings, models.data, models)
    artifact = exporter.write(request, "research-export")
    exported = pd.read_csv(models.store.root / "exports" / artifact["filename"], sep=";")
    np.testing.assert_array_equal(exported.prediction, np.floor(exporter.frame(request).value + 0.5))
    with pytest.raises(DomainError, match="Исследовательский"):
        exporter.write(request.model_copy(update={"format": "competition"}), "forbidden")


def test_d09_disabled_incomplete_factors_are_neutral():
    spec = ScenarioSpec(
        engine="recompute",
        forecast_id="base",
        route_ids=["5"],
        time_range={"start": "2026-03-01T08:00:00+03:00", "end": "2026-03-01T10:00:00+03:00"},
        enabled={"weather": False, "incidents": False, "schedule": False, "season": False},
        weather={"precipitation": -1},
        incidents=[{"route_ids": []}],
        schedule={"service_start_minute": 0},
        coefficients={"season": 1.5},
    )
    assert spec.coefficients.multiplier == 1 and not spec.incidents
    assert spec.weather.precipitation is None and spec.schedule.service_start_minute is None


def test_d18_missing_statistics_are_json_null_and_fractional_range_preserved():
    missing = describe_series([None, np.nan, np.inf])
    assert missing == {"min": None, "max": None, "mean": None, "known_hours": 0, "total_hours": 3}
    assert "NaN" not in json.dumps(missing, allow_nan=False)
    assert describe_series([0.19, 0.21])["min"] == 0.19


def test_d07_cancelled_result_is_hidden_by_both_job_endpoints(report_app):
    client, models, _, _, _, _ = report_app
    queue = WorkQueue(models.store.root)
    job = queue.enqueue("scenario_run", {"scenario_id": "cancelled-draft"}, "cancelled-result")
    queue.claim(MODEL_KINDS, "worker")
    assert client.post(f"/api/v2/jobs/{job['id']}/cancel").status_code == 200
    queue.finish(job["id"], "worker", result={"id": "already-written-result"})
    individual = client.get(f"/api/v2/jobs/{job['id']}").json()
    listed = next(j for j in client.get("/api/v2/jobs").json() if j["id"] == job["id"])
    for exposed in (individual, listed):
        assert exposed["status"] == "cancelled" and exposed["result"] is None
