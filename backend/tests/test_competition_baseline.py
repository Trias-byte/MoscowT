import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.data.schemas import ImportSpec
from moscowt.domain import DomainError
from moscowt.modeling.baseline_features import FEATURES, BaselineFeatures
from moscowt.modeling.baseline_install import install_baseline
from moscowt.modeling.competition import CompetitionAdapter
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.packages import ModelPackageService
from moscowt.modeling.service import ModelService
from moscowt.platform_exports import PeriodExporter, PeriodExportSpec, submission_columns
from moscowt.storage import SnapshotStore

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "deliverables/competition-baseline.zip"
SHA = "4adcc1b06ccd53eeddee5637560919396483809fc2da696cae3c1cba013a8d95"


def comparable(frame):
    # Parquet may restore pytz while the service uses zoneinfo for the same timezone.
    return frame.assign(timestamp=frame.timestamp.dt.tz_convert("UTC"))


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("competition")
    sources = [ROOT / "dataset/labels" / f"labels_day_{part}.csv" for part in ("train", "test")]
    if not PACKAGE.exists() or not all(p.exists() for p in sources):
        pytest.skip("Requires official labels and bundled native model")
    with zipfile.ZipFile(PACKAGE) as archive:
        spec = TrainingSpec.model_validate(json.loads(archive.read("manifest.json"))["manifest"]["spec"])
    settings = Settings(
        state_dir=root / "state",
        data_root=root / "data",
        dataset_dir=ROOT / "dataset",
        source_dir=ROOT / "dataset",
        worker_enabled=False,
    )
    data = DatasetRepository(settings.data_root)
    imported = data.preview(
        sources, ImportSpec(route_ids=spec.route_ids, time_range=spec.time_range, complete=True)
    )
    data.apply(imported["id"])
    receipt = install_baseline(settings, PACKAGE)
    service = ModelService(data, SnapshotStore(settings.state_dir))
    return settings, service, receipt


def test_golden_submission_and_package_roundtrip(baseline, tmp_path):
    settings, service, receipt = baseline
    run = service.store.read("forecast_runs", receipt["forecast_id"])
    exported = PeriodExporter(settings, service.data, service).write(
        PeriodExportSpec(
            dataset_id=receipt["dataset_id"],
            forecast_id=run["id"],
            route_ids=run["spec"]["route_ids"],
            time_range=run["spec"]["time_range"],
            mode="forecast",
            format="competition",
        ),
        "golden",
    )
    assert exported["sha256"] == SHA
    assert exported["rows"] == 14640
    assert exported["float_policy"] == "round_half_even_nonnegative_integer"
    packages = ModelPackageService(service)
    assert packages.inspect(PACKAGE)["manifest"]["spec"]["dataset_id"] == receipt["dataset_id"]
    native = tmp_path / "model.zip"
    native.write_bytes(packages.export(receipt["model_id"]))
    restored = packages.import_package(native)
    forecast = ForecastSpec.model_validate({**run["spec"], "model_id": restored["id"]})
    original = service.frame(run["id"])[1]
    pd.testing.assert_frame_equal(
        comparable(service.predict(forecast, persist=False)), comparable(original), check_exact=True
    )


def test_subset_month_chunks_future_invariance_and_horizon(baseline):
    _, service, receipt = baseline
    run, full = service.frame(receipt["forecast_id"])
    spec = ForecastSpec.model_validate(
        {
            **run["spec"],
            "route_ids": ["7"],
            "time_range": {
                "start": "2025-11-30T17:00:00+03:00",
                "end": "2025-12-02T06:00:00+03:00",
            },
        }
    )
    subset = service.predict(spec, persist=False)
    expected = full.loc[
        (full.route == "7")
        & (full.timestamp >= spec.time_range.start)
        & (full.timestamp < spec.time_range.end)
    ].reset_index(drop=True)
    pd.testing.assert_frame_equal(comparable(subset), comparable(expected), check_exact=True)
    manifest, model = service.load(receipt["model_id"])
    history = service.data.frame(receipt["dataset_id"])
    future = full.assign(value=1e12)
    poisoned = pd.concat([history, future], ignore_index=True)
    adapter = CompetitionAdapter()
    pd.testing.assert_frame_equal(adapter.predict(model, poisoned, spec), subset)
    with pytest.raises(DomainError, match="61"):
        service.predict(
            spec.model_copy(
                update={
                    "time_range": spec.time_range.model_copy(
                        update={"end": pd.Timestamp("2026-01-02T00:00:00+03:00")}
                    )
                }
            ),
            persist=False,
        )
    for mutation in (
        history.iloc[1:],
        history.assign(value=history.value.mask(history.index == 0)),
        pd.concat([history, history.iloc[[0]]]),
    ):
        with pytest.raises(DomainError, match="сетк|наблюден"):
            adapter.predict(model, mutation, spec)


def test_import_rejects_tampered_estimator_and_history(baseline, tmp_path):
    _, service, receipt = baseline
    packages = ModelPackageService(service)
    content = packages.export(receipt["model_id"])
    good = tmp_path / "good.zip"
    good.write_bytes(content)
    with zipfile.ZipFile(good) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value + b"changed" if name == "catboost_42.cbm" else value)
    with pytest.raises(DomainError, match="сумма"):
        packages.import_package(bad)
    meta = json.loads(files["manifest.json"])
    meta["parameters"]["history_sha256"] = "0" * 64
    files["manifest.json"] = json.dumps(meta).encode()
    with zipfile.ZipFile(bad, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    with pytest.raises(DomainError, match="История"):
        packages.import_package(bad)


def test_training_zero_route_is_learned_and_seed_score_not_inherited(model_data):
    data, ident, store = model_data
    history = data.frame(ident)
    # Route number 5 has positive demand here; an unrelated route has none.
    history.loc[history.route == "new-101", "value"] = 0
    spec = TrainingSpec(
        dataset_id=ident,
        model_type="competition_catboost",
        route_ids=["5", "new-101"],
        time_range={"start": "2026-01-01T00:00:00+03:00", "end": "2026-03-01T00:00:00+03:00"},
    )
    builder = BaselineFeatures(history, spec.route_ids, spec.time_range.start, spec.time_range.end)
    rows = builder.supervised()
    assert rows.groupby(["date", "hour", "horizon_days"]).route.nunique().eq(2).all()
    assert len(rows) <= 120000
    adapter = CompetitionAdapter()
    model = adapter.fit(history, spec, 2)
    forecast = ForecastSpec(
        model_id="check",
        dataset_id=ident,
        route_ids=spec.route_ids,
        origin=spec.time_range.end,
        time_range={"start": spec.time_range.end, "end": "2026-03-02T00:00:00+03:00"},
    )
    predictions = adapter.predict(model, history, forecast)
    assert predictions.loc[predictions.route == "new-101", "value"].eq(0).all()
    assert predictions.loc[predictions.route == "5", "value"].sum() > 0
    assert model["features"] == FEATURES
    assert "competition_result" not in model
    # The source training code also removes any external score when refitting.
    trained = ModelService(data, store).train(spec)
    assert "competition_result" not in trained


def test_baseline_rounding_ties():
    frame = pd.DataFrame(
        {
            "route": ["1"] * 4,
            "timestamp": pd.date_range("2025-11-01", periods=4, freq="h", tz="Europe/Moscow"),
            "value": [0.5, 1.5, 2.5, 3.5],
        }
    )
    assert submission_columns(frame, half_even=True).prediction.tolist() == [0, 2, 2, 4]
    assert submission_columns(frame).prediction.tolist() == [1, 2, 3, 4]


def test_refresh_keeps_61_day_recipe_and_drops_competition_claim(tmp_path, store):
    import shutil

    from test_model_refresh import import_rows

    from moscowt.modeling.refresh import update_payload
    from moscowt.platform_api import PublishRequest
    from moscowt.platform_worker import execute_and_finish
    from moscowt.publication import publish_forecast
    from moscowt.tasks import WorkQueue

    data = DatasetRepository(tmp_path / "data")
    dataset = data.apply(import_rows(data, tmp_path, "2026-01-01", "2026-05-01")["id"])["dataset_id"]
    local = SnapshotStore(tmp_path / "state")
    shutil.copytree(store.root / "networks", local.root / "networks")
    local.publish(networkId=store.current()["networkId"])
    settings = Settings(state_dir=local.root, data_root=data.root, worker_enabled=False, model_threads=2)
    models = ModelService(data, local, threads=2)
    model = models.train(
        TrainingSpec(
            dataset_id=dataset,
            model_type="competition_catboost",
            route_ids=["5", "17"],
            time_range={"start": "2026-01-01T00:00:00+03:00", "end": "2026-05-01T00:00:00+03:00"},
        )
    )
    forecast = models.predict(
        ForecastSpec(
            dataset_id=dataset,
            model_id=model["id"],
            route_ids=["5", "17"],
            origin="2026-05-01T00:00:00+03:00",
            time_range={"start": "2026-05-01T00:00:00+03:00", "end": "2026-07-01T00:00:00+03:00"},
        )
    )
    publish_forecast(settings, data, models, PublishRequest(forecast_id=forecast["id"]))
    update = import_rows(data, tmp_path, "2026-05-01", "2026-05-02", base=dataset)
    queue = WorkQueue(local.root)
    job = queue.enqueue(
        "model_refresh", update_payload(data, local, upload_id=update["id"]), "baseline-refresh"
    )
    execute_and_finish(settings, queue.claim(("model_refresh",), "test"))
    result = queue.get(job["id"])
    assert result["status"] == "ready", result
    result["result"] = json.loads(result["result"])
    updated = local.read("trained_models", result["result"]["model_id"])
    assert updated["spec"]["model_type"] == "competition_catboost"
    assert "competition_result" not in updated
    run = local.read("forecast_runs", result["result"]["forecast_id"])
    assert run["horizon_hours"] == 61 * 24
    assert run["rows"] == 2 * 61 * 24
    assert local.current()["forecastId"] == run["id"]
    report = local.read("reports", result["result"]["evaluation_id"])
    assert report["gate"]["passed"]
    assert report["folds"] and all(
        (pd.Timestamp(f["end"]) - pd.Timestamp(f["origin"])).days == 61 for f in report["folds"]
    )
