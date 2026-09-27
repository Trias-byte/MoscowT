"""Annual ensemble inference and durable automatic updates on actual refitted models."""

import json
import shutil

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from moscowt.api import create_app
from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.data.schemas import ImportSpec
from moscowt.domain import DomainError
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.coverage import dataset_coverage
from moscowt.modeling.packages import EnsembleWeights, ModelPackageService
from moscowt.modeling.refresh import ModelRefresh, aggregate_score, quality_gate, update_payload
from moscowt.modeling.service import ModelService
from moscowt.platform_api import PublishRequest
from moscowt.platform_worker import execute_and_finish
from moscowt.publication import publish_forecast
from moscowt.storage import SnapshotStore, file_hash
from moscowt.tasks import WorkQueue


def import_rows(
    data, tmp_path, start, end, *, base=None, multiplier=1, mode="append", padded_end=None, routes=("5", "17")
):
    times = pd.date_range(start, end, freq="h", inclusive="left", tz="Europe/Moscow")
    source = tmp_path / f"history-{start}-{multiplier}.csv"
    source.write_text(
        "route;date;hour;boardings\n"
        + "\n".join(
            f"{route};{t.date()};{t.hour};{round(multiplier * (30 + t.hour + t.dayofweek * 3 + int(route)))}"
            for route in routes
            for t in times
        )
    )
    spec = ImportSpec(
        base_dataset_id=base,
        mode=mode,
        time_range={"start": times[0], "end": (padded_end or end) + "T00:00:00+03:00"},
    )
    return data.preview([source], spec)


@pytest.fixture
def refresh_env(tmp_path, store):
    data = DatasetRepository(tmp_path / "data")
    ident = data.apply(import_rows(data, tmp_path, "2026-01-01", "2026-05-01")["id"])["dataset_id"]
    local = SnapshotStore(tmp_path / "state")
    shutil.copytree(store.root / "networks", local.root / "networks")
    local.publish(networkId=store.current()["networkId"])
    settings = Settings(state_dir=local.root, data_root=data.root, worker_enabled=False, model_threads=1)
    models = ModelService(data, local, threads=1)
    training = TrainingSpec(
        dataset_id=ident,
        model_type="lgb_cb_rf",
        route_ids=["5", "17"],
        time_range={"start": "2026-01-01T00:00:00+03:00", "end": "2026-05-01T00:00:00+03:00"},
        parameters={"cat_iterations": 3, "lgb_estimators": 3, "rf_estimators": 3},
    )
    model = models.train(training)
    base = models.predict(
        ForecastSpec(
            dataset_id=ident,
            model_id=model["id"],
            route_ids=training.route_ids,
            origin=training.time_range.end,
            time_range={"start": training.time_range.end, "end": "2026-06-01T00:00:00+03:00"},
        )
    )
    publish_forecast(settings, data, models, PublishRequest(forecast_id=base["id"]))
    return settings, data, models, model, base


def queued(env, preview):
    settings, data, models, _, _ = env
    queue = WorkQueue(models.store.root)
    queue.enqueue("model_refresh", update_payload(data, models.store, upload_id=preview["id"]), preview["id"])
    return queue, queue.claim(("model_refresh",), "test")


def test_annual_ensemble_chunks_match_short_release_and_calendar_limits(refresh_env):
    settings, data, models, model, base = refresh_env
    spec = ForecastSpec.model_validate(base["spec"])
    yearly = spec.model_copy(
        update={
            "time_range": spec.time_range.model_copy(
                update={"end": pd.Timestamp("2027-05-01", tz="Europe/Moscow")}
            )
        }
    )
    run = models.predict(yearly)
    frame = models.frame(run["id"])[1]
    assert len(frame) == 2 * 8760 and np.isfinite(frame.value).all() and frame.value.ge(0).all()
    from moscowt.platform_exports import PeriodExporter, PeriodExportSpec

    exported = PeriodExporter(settings, data, models).write(
        PeriodExportSpec(
            dataset_id=model["spec"]["dataset_id"],
            forecast_id=run["id"],
            route_ids=["5", "17"],
            time_range={"start": "2027-04-01T00:00:00+03:00", "end": "2027-05-01T00:00:00+03:00"},
            mode="auto",
            format="submission",
        ),
        "annual-last-month",
    )
    assert exported["rows"] == 2 * 30 * 24
    np.testing.assert_allclose(
        models.frame(base["id"])[1].value, frame.loc[frame.timestamp < spec.time_range.end].value, rtol=1e-12
    )
    for origin, end, hours in [("2028-01-01", "2029-01-01", 8784), ("2028-02-29", "2029-02-28", 8760)]:
        later = yearly.model_copy(
            update={
                "origin": pd.Timestamp(origin, tz="Europe/Moscow"),
                "time_range": spec.time_range.model_copy(
                    update={
                        "start": pd.Timestamp(origin, tz="Europe/Moscow"),
                        "end": pd.Timestamp(end, tz="Europe/Moscow"),
                    }
                ),
            }
        )
        assert models.predict(later)["rows"] == 2 * hours
        with pytest.raises(DomainError, match="календарный|Максимум"):
            models.predict(
                later.model_copy(
                    update={
                        "time_range": later.time_range.model_copy(
                            update={"end": later.time_range.end + pd.Timedelta(hours=1)}
                        )
                    }
                )
            )
    for days in (62, 63):
        assert (
            models.predict(
                yearly.model_copy(
                    update={
                        "time_range": spec.time_range.model_copy(
                            update={"end": spec.origin + pd.Timedelta(days=days)}
                        )
                    }
                )
            )["rows"]
            == 2 * days * 24
        )
    with TestClient(create_app(settings)) as client:
        options = client.get("/api/v1/capabilities").json()
        assert "year" in options["horizons"]
        assert any(o["id"] == run["id"] and o["kind"] == "primary" for o in options["forecastOptions"])


def test_real_import_refits_all_estimators_and_publishes_without_changing_old_versions(refresh_env, tmp_path):
    settings, data, models, model, base = refresh_env
    weights = EnsembleWeights(catboost=0.5, lightgbm=0.2, random_forest=0.3)
    weighted = ModelPackageService(models).reweight(model["id"], weights)
    base = models.predict(ForecastSpec.model_validate({**base["spec"], "model_id": weighted["id"]}))
    publish_forecast(settings, data, models, PublishRequest(forecast_id=base["id"]))
    old_hashes = {
        p: file_hash(p)
        for kind in ("trained_models", "forecast_runs")
        for p in (models.store.root / kind).iterdir()
    }
    preview = import_rows(
        data,
        tmp_path,
        "2026-05-01",
        "2026-06-01",
        base=model["spec"]["dataset_id"],
        multiplier=1.3,
        padded_end="2026-07-01",
    )
    queue, job = queued(refresh_env, preview)
    execute_and_finish(settings, job)
    done = queue.public(queue.get(job["id"]))
    assert done["status"] == "ready", done
    result = done["result"]
    assert result["publication_status"] == "published"
    assert result["origin"] == "2026-06-01T00:00:00+03:00"  # not padded July
    assert result["model_id"] not in (model["id"], weighted["id"])
    new, fitted = models.load(result["model_id"])
    assert new["ensemble_weights"] == weights.model_dump()
    assert new["spec"]["parameters"] == model["spec"]["parameters"]
    assert new["spec"]["time_range"]["start"] == model["spec"]["time_range"]["start"]
    assert [w for w, _ in fitted["models"]] == [0.5, 0.2, 0.3]
    assert new["training_rows"] > model["training_rows"]
    forecast, values = models.frame(result["forecast_id"])
    assert forecast["rows"] == 2 * 8760 and forecast["model_type"] == "lgb_cb_rf"
    assert values.value.mean() != pytest.approx(models.frame(base["id"])[1].value.mean())
    assert models.store.current()["forecastId"] == forecast["id"]
    assert data.catalog.current_id() == result["dataset_id"]
    report = models.store.read("reports", result["evaluation_id"])
    assert report["gate"]["passed"] and len(report["folds"]) == 3
    assert next(h for h in report["horizons"] if h["days"] == 365)["status"] == "insufficient_history"
    for fold in report["folds"]:
        for side in ("baseline", "candidate"):
            trained = models.store.read("trained_models", fold[side + "_model_id"])
            assert pd.Timestamp(trained["spec"]["time_range"]["end"]) <= pd.Timestamp(fold["origin"])
    assert all(file_hash(p) == sha for p, sha in old_hashes.items())
    coverage = dataset_coverage(str(data.root), result["dataset_id"])
    assert coverage["forecast_origin"] == result["origin"]
    # Same imported values do not retrain, even with an explicitly requested new update.
    second = queue.enqueue(
        "model_refresh", update_payload(data, models.store, dataset_id=result["dataset_id"]), "repeat"
    )
    execute_and_finish(settings, queue.claim(("model_refresh",), "test"))
    assert queue.public(queue.get(second["id"]))["result"]["duplicate"]
    assert models.store.current()["forecastId"] == forecast["id"]


def test_quality_gate_threshold_and_zero_targets():
    base = {"rows": 24, "value": 0.2, "metric": "wape"}
    assert quality_gate(base, {**base, "value": 0.21})["passed"]
    assert not quality_gate(base, {**base, "value": 0.211})["passed"]
    assert aggregate_score([{"rows": 2, "absolute_truth": 0, "absolute_error": 6}]) == {
        "rows": 2,
        "metric": "mae",
        "value": 3,
    }
    assert not quality_gate({"rows": 0}, {"rows": 0})["passed"]


def test_blocked_quality_and_cancelled_update_keep_current(refresh_env, tmp_path, monkeypatch):
    settings, data, models, model, _ = refresh_env
    preview = import_rows(
        data, tmp_path, "2026-05-01", "2026-06-01", base=model["spec"]["dataset_id"], multiplier=2
    )
    queue, job = queued(refresh_env, preview)
    before = file_hash(models.store.root / "current.json")

    def reject(self, training):
        report = {"id": "validation-rejected", "gate": {"passed": False, "reason": "QUALITY_REGRESSION"}}
        self.store.put("reports", report["id"], report)
        return report

    monkeypatch.setattr(ModelRefresh, "evaluation", reject)
    execute_and_finish(settings, job)
    failed = queue.public(queue.get(job["id"]))
    assert failed["status"] == "failed" and failed["result"] is None
    assert failed["update"]["evaluation_id"] == "validation-rejected"
    assert file_hash(models.store.root / "current.json") == before
    queue.retry(job["id"])
    running = queue.claim(("model_refresh",), "other")
    queue.cancel(job["id"])
    execute_and_finish(settings, running)
    assert queue.public(queue.get(job["id"]))["status"] == "cancelled"
    assert file_hash(models.store.root / "current.json") == before


def test_api_apply_defaults_and_retry_retain_checkpoint(refresh_env, tmp_path):
    settings, data, models, model, _ = refresh_env
    preview = import_rows(data, tmp_path, "2026-05-01", "2026-06-01", base=model["spec"]["dataset_id"])
    queue = WorkQueue(models.store.root)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            f"/api/v2/uploads/{preview['id']}/apply", json={}, headers={"Idempotency-Key": "auto"}
        )
        assert response.status_code == 202 and response.json()["kind"] == "model_refresh"
        explicit = client.post(
            f"/api/v2/uploads/{preview['id']}/apply",
            json={"update_forecast": False},
            headers={"Idempotency-Key": "manual"},
        )
        assert explicit.json()["kind"] == "import_apply"
        job = queue.claim(("model_refresh",), "first")
        queue.checkpoint(job["id"], "first", "training", 0.5, dataset_id="saved-before-restart")
        queue.interrupted(job["id"], "first")
        replay = queue.claim(("model_refresh",), "second")
        assert json.loads(replay["checkpoint"])["dataset_id"] == "saved-before-restart"
        with pytest.raises(DomainError):
            queue.checkpoint(job["id"], "first", "publication", 0.95)
        queue.finish(job["id"], "second", error="interrupted")
        assert client.post(f"/api/v2/jobs/{job['id']}/retry").status_code == 202
        assert json.loads(queue.get(job["id"])["checkpoint"])["dataset_id"] == "saved-before-restart"
        assert (
            client.get(f"/api/v2/datasets/{model['spec']['dataset_id']}").json()["forecast_origin"]
            == "2026-05-01T00:00:00+03:00"
        )


def test_new_publication_blocks_stale_update_before_import(refresh_env, tmp_path):
    settings, data, models, model, _ = refresh_env
    preview = import_rows(data, tmp_path, "2026-05-01", "2026-06-01", base=model["spec"]["dataset_id"])
    queue, job = queued(refresh_env, preview)
    models.store.publish(scheduleScenario=True)
    before = file_hash(models.store.root / "current.json")
    execute_and_finish(settings, job)
    assert queue.public(queue.get(job["id"]))["update"]["error_code"] == "SNAPSHOT_CHANGED"
    assert file_hash(models.store.root / "current.json") == before
    assert data.catalog.imported(preview["fingerprint"]) is None


def test_resume_after_refit_reuses_checkpoint_and_publishes(refresh_env, tmp_path, monkeypatch):
    settings, data, models, model, _ = refresh_env
    preview = import_rows(
        data, tmp_path, "2026-05-01", "2026-06-01", base=model["spec"]["dataset_id"], multiplier=1.3
    )
    queue, job = queued(refresh_env, preview)
    original_stage = ModelRefresh.stage

    def interrupt(self, phase, progress, **changes):
        original_stage(self, phase, progress, **changes)
        if "model_id" in changes:
            raise RuntimeError("Simulated process stop after durable refit")

    monkeypatch.setattr(ModelRefresh, "stage", interrupt)
    execute_and_finish(settings, job)
    assert queue.get(job["id"])["status"] == "failed"
    checkpoint = json.loads(queue.get(job["id"])["checkpoint"])
    assert checkpoint["model_id"] and checkpoint["evaluation_id"] and not checkpoint.get("forecast_id")
    model_hash = file_hash(models.store.path("trained_models", checkpoint["model_id"], "joblib"))
    monkeypatch.setattr(ModelRefresh, "stage", original_stage)
    monkeypatch.setattr(ModelRefresh, "fit", lambda *_: pytest.fail("Completed refit must not run twice"))
    queue.retry(job["id"])
    execute_and_finish(settings, queue.claim(("model_refresh",), "replacement"))
    assert queue.get(job["id"])["status"] == "ready"
    assert file_hash(models.store.path("trained_models", checkpoint["model_id"], "joblib")) == model_hash


def test_new_route_with_short_history_is_explained_and_does_not_move_origin(refresh_env, tmp_path):
    settings, data, models, model, _ = refresh_env
    preview = import_rows(
        data, tmp_path, "2026-06-01", "2026-06-04", base=model["spec"]["dataset_id"], routes=("999",)
    )
    added = data.apply(preview["id"])["dataset_id"]
    queue, job = queued(refresh_env, preview)
    training, _, excluded = ModelRefresh(settings, data, models, job).prepare(added)
    assert "999" in excluded and "999" not in training.route_ids
    assert training.time_range.end.isoformat() == "2026-05-01T00:00:00+03:00"


def test_hourly_weather_release_only_replaces_covered_first_day(refresh_env):
    from moscowt.external import WEATHER_FIELDS, OpenMeteoForecastProvider
    from moscowt.factors import WEATHER_COLUMNS, FactorRepository, hourly_weather

    settings, data, _, _, _ = refresh_env
    times = pd.date_range("2025-01-01", "2026-01-01", freq="h", inclusive="left", tz="Europe/Moscow")
    climate = pd.DataFrame({"timestamp": times, **{key: 2.0 for key in WEATHER_COLUMNS}})
    archive = FactorRepository(data.root).save(
        "weather_hourly", climate, {"source": "controlled-test-climate"}
    )
    origin = pd.Timestamp("2026-05-01T00:00:00+03:00")
    release_hours = pd.date_range(origin, periods=48, freq="h")
    payload = {
        "daily": {"time": ["2026-05-01", "2026-05-02"], **{key: [10, 10] for key in WEATHER_FIELDS}},
        "hourly": {
            "time": release_hours.strftime("%Y-%m-%dT%H:%M").tolist(),
            **{key: [10] * 48 for key in WEATHER_COLUMNS},
        },
    }
    provider = OpenMeteoForecastProvider(data.root)
    release = provider.register(payload, origin - pd.Timedelta(hours=1), "test://weather")
    target = pd.DataFrame({"timestamp": pd.date_range(origin, periods=72, freq="h")})
    actual = hourly_weather(str(data.root), archive["id"], target, origin, forecast_id=release["id"])
    assert actual.iloc[:24].eq(10).all().all()
    assert actual.iloc[24:].eq(2).all().all()
    late = provider.register(payload, origin + pd.Timedelta(hours=1), "test://late")
    with pytest.raises(DomainError, match="после origin"):
        hourly_weather(str(data.root), archive["id"], target, origin, forecast_id=late["id"])


def test_correction_of_old_hours_refits_and_insufficient_validation_blocks(refresh_env, tmp_path):
    settings, data, models, model, _ = refresh_env
    correction = import_rows(
        data,
        tmp_path,
        "2026-01-01",
        "2026-05-01",
        base=model["spec"]["dataset_id"],
        multiplier=1.3,
        mode="upsert",
    )
    queue, job = queued(refresh_env, correction)
    execute_and_finish(settings, job)
    done = queue.public(queue.get(job["id"]))
    assert done["status"] == "ready", done
    assert done["result"]["model_id"] != model["id"]
    assert done["result"]["origin"] == "2026-05-01T00:00:00+03:00"
    next_job = queue.enqueue(
        "model_refresh",
        update_payload(data, models.store, dataset_id=done["result"]["dataset_id"]),
        "short-validation",
    )
    refresh = ModelRefresh(settings, data, models, queue.claim(("model_refresh",), "test"))
    from moscowt.domain import TimeRange

    short = refresh.original.model_copy(
        update={"time_range": TimeRange(start="2026-01-01T00:00:00+03:00", end="2026-03-01T00:00:00+03:00")}
    )
    report = refresh.evaluation(short)
    assert report["gate"]["reason"] == "INSUFFICIENT_VALIDATION" and not report["folds"]
    queue.cancel(next_job["id"])


@pytest.mark.parametrize("interruption", ["cancel", "new_publication"])
def test_publication_commit_rechecks_cancellation_and_snapshot(refresh_env, tmp_path, interruption):
    settings, data, models, model, base = refresh_env
    queue, job = queued(
        refresh_env, import_rows(data, tmp_path, "2026-05-01", "2026-06-01", base=model["spec"]["dataset_id"])
    )
    refresh = ModelRefresh(settings, data, models, job)
    refresh.stage("publication", 0.95, dataset_id=model["spec"]["dataset_id"], forecast_id=base["id"])
    if interruption == "cancel":
        queue.cancel(job["id"])
    else:
        models.store.publish(scheduleScenario=True)
    before = file_hash(models.store.root / "current.json")
    with pytest.raises(DomainError) as error:
        refresh.commit()
    assert error.value.code == ("JOB_INTERRUPTED" if interruption == "cancel" else "SNAPSHOT_CHANGED")
    assert file_hash(models.store.root / "current.json") == before


def test_missing_weather_archive_stops_refresh_without_replacing_publication(
    refresh_env, tmp_path, monkeypatch
):
    from moscowt.factors import WEATHER_COLUMNS, FactorRepository

    settings, data, models, model, _ = refresh_env
    queue, job = queued(
        refresh_env, import_rows(data, tmp_path, "2026-05-01", "2026-06-01", base=model["spec"]["dataset_id"])
    )
    refresh = ModelRefresh(settings, data, models, job)
    weather = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=48, freq="h", tz="Europe/Moscow"),
            **{key: 2.0 for key in WEATHER_COLUMNS},
        }
    )
    archive = FactorRepository(data.root).save("weather_hourly", weather, {"source": "test://short-archive"})
    refresh.original = refresh.original.model_copy(
        update={"feature_groups": ["calendar", "weather"], "weather_hourly_id": archive["id"]}
    )
    monkeypatch.setattr(
        FactorRepository, "fetch_weather", lambda *_: (_ for _ in ()).throw(OSError("Archive unavailable"))
    )
    before = file_hash(models.store.root / "current.json")
    with pytest.raises(DomainError) as error:
        refresh.prepare(model["spec"]["dataset_id"])
    assert error.value.code == "WEATHER_REFRESH_FAILED"
    assert file_hash(models.store.root / "current.json") == before
