from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from moscowt.analytics import RouteAnalyticsService
from moscowt.domain import HISTORY_END, TZ, Scope
from moscowt.forecasting import (
    evaluate,
    fit_model,
    predict,
    profile_prediction,
    seasonal_profile,
    target_grid,
)
from moscowt.pipelines import build_history, prepare_labels


def test_real_labels_grid_and_idempotence(store, dataset):
    before = store.current()
    manifest = prepare_labels(store, dataset)
    assert manifest["checks"]["keys"] == 72960
    assert manifest["checks"]["sum"] == 59667191
    assert manifest["checks"]["filledZeros"] == 15409
    assert manifest["checks"]["rawAuditExecuted"] is False
    frame = pd.read_parquet(store.path("histories", manifest["id"], "parquet"))
    assert len(frame[frame.route == 5]) == 7296
    assert frame[frame.route == 5].value.sum() == 0
    day = frame[(frame.route == 50) & (frame.timestamp.dt.strftime("%Y-%m-%d") == "2025-09-21")]
    assert day.value.sum() == 0 and (day.qualityFlag == "requires_review").all()
    assert frame.allOperations.isna().all() and frame.failedOperations.isna().all()
    assert store.current() == before


@pytest.mark.parametrize(
    "edit",
    [
        lambda f: pd.concat([f, f.iloc[:1]]),
        lambda f: f.assign(hour=24),
        lambda f: f.assign(boardings=-1),
        lambda f: f.assign(route=13),
        lambda f: f.assign(date="2025-11-01"),
    ],
)
def test_invalid_labels_rejected(edit):
    labels = pd.DataFrame({"route": [1], "date": ["2025-01-01"], "hour": [0], "boardings": [1]})
    with pytest.raises(ValueError):
        build_history(edit(labels))


def test_moscow_hour_and_absent_future(store, scope):
    scope.update(mode="history", timeRange={"start": "2025-10-31T21:00:00Z", "end": "2025-10-31T22:00:00Z"})
    result = RouteAnalyticsService(store).view(Scope.model_validate(scope))
    assert result["frames"][0]["start"] == "2025-11-01T00:00:00+03:00"
    assert all(v["value"] is None and v["coverageStatus"] == "missing" for v in result["frames"][0]["values"])
    assert result["frames"][0]["aggregate"] is None


def test_fixed_forecast_has_no_future_inputs(store):
    history = pd.read_parquet(store.path("histories", store.current()["historyId"], "parquet"))
    origin = datetime(2025, 9, 1, tzinfo=TZ)
    original = seasonal_profile(history, origin)
    modified = history.copy()
    modified.loc[modified.timestamp >= origin, "value"] = 999999999
    assert seasonal_profile(modified, origin) == original
    pred, _ = profile_prediction(target_grid(origin, 61), original)
    assert len(pred) == 14640 and np.isfinite(pred).all()
    artifact = fit_model(store, store.current()["historyId"], HISTORY_END, "seasonal")
    with pytest.raises(Exception, match="моменту выпуска"):
        predict(store, artifact, origin, 61)
    frozen = predict(store, artifact, HISTORY_END, 61)
    assert frozen[frozen.route == 5].value.sum() == 0
    assert len(frozen) == 14640
    assert datetime.fromisoformat(artifact["trainingEnd"]) <= HISTORY_END


def test_wape_is_pooled_and_zero_is_undefined():
    assert evaluate([0, 0], [1, 2]) == {"absoluteError": 3.0, "actualSum": 0.0, "wape": None, "score": None}
    assert evaluate([1, 99], [0, 99])["wape"] == 0.01


def test_snapshot_publication_is_immutable(store):
    snapshot = store.current()
    with pytest.raises(Exception, match="неизменяема"):
        store.put("snapshots", snapshot["snapshotId"], {"changed": True})
    assert store.read("snapshots", snapshot["snapshotId"]) == snapshot


@pytest.mark.parametrize("grain", ["hour", "day"])
def test_auto_prefers_observed_zero_and_falls_back_per_hour(store, scope, monkeypatch, grain):
    service = RouteAnalyticsService(store)
    snapshot = store.current()
    forecast = service.matrix("forecasts", snapshot["forecastId"])
    matrices = {}
    for kind in ("histories", "forecasts"):
        matrix = {**forecast, "count": 24}
        for field in ("value", "baseline", "baselineCount", "valueOrigin", "coverageStatus", "qualityFlag"):
            matrix[field] = forecast[field][:, :24].copy()
        matrix["value"][:] = np.nan if kind == "histories" else 10
        matrix["valueOrigin"][:] = "label" if kind == "histories" else "model"
        matrix["baseline"][:] = 3 if kind == "histories" else 8
        matrices[kind] = matrix
    matrices["histories"]["value"][0, :3] = [0, np.nan, 12]
    monkeypatch.setattr(service, "matrix", lambda kind, ident: matrices[kind])
    result = service.view(Scope.model_validate({**scope, "mode": "auto", "grain": grain, "routeIds": ["1"]}))
    values = [f["values"][0] for f in result["frames"]]
    assert result["meta"]["provenance"] == "mixed"
    if grain == "hour":
        assert [v["value"] for v in values[:3]] == [0, 10, 12]
        assert [v["provenance"] for v in values[:3]] == ["observation", "forecast", "observation"]
        assert [v["baseline"] for v in values[:3]] == [3, 8, 3]
    else:
        assert values[0]["value"] == 232
        assert values[0]["provenance"] == "mixed"
        assert values[0]["valueOrigins"] == ["label", "model"]


def test_auto_without_any_source_keeps_missing(store, scope):
    scope.update(
        mode="auto",
        timeRange={
            "start": "2026-01-01T00:00:00+03:00",
            "end": "2026-01-02T00:00:00+03:00",
        },
    )
    result = RouteAnalyticsService(store).view(Scope.model_validate(scope))
    assert result["meta"]["provenance"] == "missing"
    assert all(f["aggregate"] is None for f in result["frames"])
