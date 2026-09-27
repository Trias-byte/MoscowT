import zipfile

import numpy as np
import pandas as pd
import pytest

from moscowt.bundles import create_bundle, restore_bundle
from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.data.schemas import ImportSpec
from moscowt.domain import DomainError
from moscowt.factors import FactorRepository, hourly_weather
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.packages import EnsembleWeights, ModelPackageService
from moscowt.modeling.service import ModelService
from moscowt.platform_exports import PeriodExporter, PeriodExportSpec
from moscowt.scenario_engine import ScenarioForecastService, availability, demand_ratio, service_fraction
from moscowt.scenarios import ScenarioService, ScenarioSpec
from moscowt.storage import SnapshotStore
from moscowt.tasks import MODEL_KINDS, WorkQueue


def training(ident, kind="seasonal", **changes):
    return TrainingSpec(
        dataset_id=ident,
        model_type=kind,
        route_ids=["5", "new-101"],
        time_range={"start": "2026-01-01T00:00:00+03:00", "end": "2026-03-01T00:00:00+03:00"},
        parameters={"cat_iterations": 8, "lgb_estimators": 8, "rf_estimators": 8}
        if kind == "lgb_cb_rf"
        else {},
        **changes,
    )


def base_run(model_data, kind="seasonal", **changes):
    data, ident, store = model_data
    models = ModelService(data, store, 1)
    model = models.train(training(ident, kind, **changes))
    spec = ForecastSpec(
        model_id=model["id"],
        dataset_id=ident,
        origin="2026-03-01T00:00:00+03:00",
        route_ids=["5", "new-101"],
        time_range={"start": "2026-03-01T00:00:00+03:00", "end": "2026-03-02T00:00:00+03:00"},
    )
    run = models.predict(spec)
    return models, spec, run


def execute_scenario(model_data, models, run, **changes):
    data, ident, store = model_data
    settings = Settings(state_dir=store.root, data_root=data.root, worker_enabled=False)
    spec = ScenarioSpec(
        forecast_id=run["id"],
        engine="recompute",
        route_ids=["5"],
        time_range={"start": "2026-03-01T08:00:00+03:00", "end": "2026-03-01T10:00:00+03:00"},
        **changes,
    )
    draft = ScenarioService(store).create(spec)
    queue = WorkQueue(store.root)
    queued = queue.enqueue("scenario_run", {"scenario_id": draft["id"]}, draft["id"])
    job = queue.claim(MODEL_KINDS, "test")
    result = ScenarioForecastService(settings, data, models).run(draft["id"], queued["id"])
    return settings, queue, job, result


def test_named_datasets_do_not_mix_or_activate(model_data, tmp_path):
    data, ident, _ = model_data
    source = tmp_path / "independent.csv"
    source.write_text("route;date;hour;boardings\n5;2026-01-01;0;999\n")
    spec = ImportSpec(
        new_dataset=True,
        dataset_name="Другая выборка",
        time_range={"start": "2026-01-01T00:00:00+03:00", "end": "2026-01-02T00:00:00+03:00"},
    )
    first = data.apply(data.preview([source], spec)["id"])
    assert data.catalog.current_id() == ident
    assert data.frame(first["dataset_id"]).value.sum() == 999
    assert data.manifest(first["dataset_id"])["name"] == "Другая выборка"
    assert data.apply(data.preview([source], spec)["id"])["duplicate"]
    branch = spec.model_copy(update={"new_dataset": False, "base_dataset_id": ident, "mode": "upsert"})
    second = data.apply(data.preview([source], branch)["id"])
    assert data.manifest(second["dataset_id"])["parent_id"] == ident
    assert data.frame(second["dataset_id"]).value.sum() > 999
    assert data.catalog.current_id() == ident


def test_interval_union_night_hours_and_zero_service():
    t = pd.Timestamp("2025-01-01T23:00:00+03:00")
    intervals = [
        (t, t + pd.Timedelta(minutes=45), 0.5),
        (t + pd.Timedelta(minutes=15), t + pd.Timedelta(hours=1), 1),
    ]
    assert availability(t, t + pd.Timedelta(hours=1), intervals) == 0.125
    assert service_fraction(t, 23 * 60 + 30, 60) == 0.5
    assert service_fraction(t + pd.Timedelta(hours=1), 23 * 60 + 30, 60) == 1
    assert service_fraction(t + pd.Timedelta(hours=2), 23 * 60 + 30, 60) == 0
    assert demand_ratio(0, 0) == 0


def test_neutral_result_is_identical_and_cancelled_result_invisible(model_data, tmp_path):
    models, forecast_spec, run = base_run(model_data)
    settings, queue, job, result = execute_scenario(model_data, models, run)
    data, ident, store = model_data
    request = PeriodExportSpec(
        dataset_id=ident,
        forecast_id=run["id"],
        route_ids=["5", "new-101"],
        time_range=forecast_spec.time_range,
        mode="forecast",
    )
    exporter = PeriodExporter(settings, data, models)
    original = exporter.frame(request)
    scenario_request = request.model_copy(update={"scenario_id": result["id"]})
    with pytest.raises(DomainError, match="не завершён"):
        exporter.frame(scenario_request)
    queue.finish(job["id"], "test", result=result)
    adjusted = exporter.frame(scenario_request)
    np.testing.assert_array_equal(original.value, adjusted.value)
    assert adjusted.vehicle_hours.isna().all()  # unknown fleet stays unknown
    # A retry after result publication but before queue acknowledgement is idempotent.
    retried = ScenarioForecastService(settings, data, models).run(result["draft_id"], job["id"])
    assert retried == result
    queue2_spec = ScenarioSpec.model_validate(result["spec"]).model_copy(update={"name": "cancel"})
    draft = ScenarioService(store).create(queue2_spec)
    pending = queue.enqueue("scenario_run", {"scenario_id": draft["id"]}, "cancel")
    running = queue.claim(MODEL_KINDS, "test")
    queue.cancel(pending["id"])
    cancelled_result = ScenarioForecastService(settings, data, models).run(draft["id"], running["id"])
    queue.finish(running["id"], "test", result=cancelled_result)
    with pytest.raises(DomainError, match="не завершён"):
        ScenarioService(store).apply(original, cancelled_result["id"], run["id"])
    bundle = create_bundle(settings, "completed-only")
    restored_settings = Settings(state_dir=tmp_path / "restored-state", data_root=tmp_path / "restored-data")
    restore_bundle(store.root / "exports" / bundle["filename"], restored_settings)
    restored_store = SnapshotStore(restored_settings.state_dir)
    restored_data = DatasetRepository(restored_settings.data_root)
    restored = PeriodExporter(
        restored_settings, restored_data, ModelService(restored_data, restored_store, 1)
    )
    np.testing.assert_array_equal(restored.frame(scenario_request).value, adjusted.value)
    assert not restored_store.path("scenarios", cancelled_result["id"]).exists()
    path = store.path("scenario_results", result["id"], "parquet")
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(DomainError, match="Контрольная сумма"):
        exporter.frame(scenario_request)


def test_incident_schedule_overlap_and_csv_share_values(model_data):
    models, fs, run = base_run(model_data)
    incident = {
        "id": "crash",
        "longitude": 37.62,
        "latitude": 55.75,
        "route_ids": ["5"],
        "start": "2026-03-01T08:30:00+03:00",
        "duration_minutes": 90,
        "reduction": 1,
    }
    settings, queue, job, result = execute_scenario(
        model_data,
        models,
        run,
        incidents=[incident, {**incident, "id": "same-location"}],
        schedule={"base_headway_minutes": 10, "headway_minutes": 5, "elasticity": 0.3},
    )
    queue.finish(job["id"], "test", result=result)
    request = PeriodExportSpec(
        dataset_id=fs.dataset_id,
        forecast_id=run["id"],
        route_ids=fs.route_ids,
        time_range=fs.time_range,
        mode="forecast",
        scenario_id=result["id"],
    )
    exporter = PeriodExporter(settings, models.data, models)
    rows = exporter.frame(request)
    selected = rows.loc[rows.route.eq("5") & rows.timestamp.dt.hour.isin([8, 9])]
    assert selected.iloc[0].value == pytest.approx(
        selected.iloc[0].base_value
    )  # 2x frequency * half hour closure = 1
    assert selected.iloc[1].value == 0
    other = rows.loc[rows.route.eq("new-101")]
    np.testing.assert_array_equal(other.value, other.base_value)
    artifact = exporter.write(request, "test-export")
    exported = pd.read_csv(models.store.root / "exports" / artifact["filename"], sep=";")
    np.testing.assert_allclose(exported.value, rows.value)
    season_neutral = next(p for p in result["sensitivity"]["season"] if p["x"] == 1)
    assert season_neutral["value"] == pytest.approx(result["total"])


def test_portable_roundtrip_and_reweight(model_data, tmp_path):
    models, fs, run = base_run(model_data, "lgb_cb_rf")
    packages = ModelPackageService(models)
    path = tmp_path / "model.zip"
    path.write_bytes(packages.export(fs.model_id))
    imported = packages.import_package(path)
    prediction = models.predict(fs.model_copy(update={"model_id": imported["id"]}), persist=False)
    np.testing.assert_allclose(prediction.value, models.frame(run["id"])[1].value, rtol=1e-10)
    changed = packages.reweight(imported["id"], EnsembleWeights(catboost=1, lightgbm=0, random_forest=0))
    assert changed["parent_model_id"] == imported["id"]
    assert models.load(fs.model_id)[0]["id"] == fs.model_id
    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    contents["lightgbm.txt"] = b"corrupted"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    with pytest.raises(DomainError, match="контрольная"):
        packages.inspect(path)


def test_weather_enters_inference_and_future_facts_do_not(model_data):
    data, _, _ = model_data
    times = pd.date_range("2025-01-01", "2026-03-03", freq="h", inclusive="left", tz="Europe/Moscow")
    weather = pd.DataFrame(
        {
            "timestamp": times,
            "temperature_2m": times.hour.astype(float),
            "relative_humidity_2m": np.full(len(times), 60.0),
            "precipitation": np.zeros(len(times)),
        }
    )
    archive = FactorRepository(data.root).save("weather_hourly", weather, {"test": 1})
    models, spec, _ = base_run(
        model_data, "lgb_cb_rf", weather_hourly_id=archive["id"], feature_groups=["weather"]
    )
    override = {
        "route_ids": ["5"],
        "start": spec.time_range.start,
        "end": spec.time_range.end,
        "weather": {"temperature_2m": 50},
    }
    base = models.predict(spec, persist=False)
    modified = models.predict(spec, overrides=override, persist=False)
    assert (
        base.loc[base.route.eq("5"), "value"].to_numpy()
        != modified.loc[modified.route.eq("5"), "value"].to_numpy()
    ).any()
    np.testing.assert_array_equal(
        base.loc[base.route.eq("new-101"), "value"], modified.loc[modified.route.eq("new-101"), "value"]
    )
    target = pd.DataFrame({"timestamp": [pd.Timestamp("2026-03-01T08:00:00+03:00")]})
    before = hourly_weather(str(data.root), archive["id"], target, spec.origin)
    weather.loc[weather.timestamp >= spec.origin, "temperature_2m"] = -50
    second = FactorRepository(data.root).save("weather_hourly", weather, {"test": 2})
    after = hourly_weather(str(data.root), second["id"], target, spec.origin)
    pd.testing.assert_frame_equal(before, after)
