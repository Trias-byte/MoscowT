import numpy as np
import pandas as pd
import pytest

from moscowt.data.repository import DatasetRepository
from moscowt.domain import DomainError
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.features import FeatureBuilder
from moscowt.modeling.service import ModelService
from moscowt.storage import SnapshotStore


def specs(ident, model_type):
    training = TrainingSpec(
        dataset_id=ident,
        model_type=model_type,
        route_ids=["5", "new-101"],
        time_range={"start": "2026-01-01T00:00:00+03:00", "end": "2026-03-01T00:00:00+03:00"},
        parameters={"lgb_estimators": 3, "cat_iterations": 3, "rf_estimators": 3}
        if model_type == "lgb_cb_rf"
        else {},
    )
    return training


@pytest.mark.parametrize("adapter", ["seasonal", "lgb_cb_rf"])
def test_adapter_restart_period_and_new_route(model_data, adapter):
    data, ident, store = model_data
    service = ModelService(data, store, threads=1)
    training = specs(ident, adapter)
    model = service.train(training)
    spec = ForecastSpec(
        model_id=model["id"],
        dataset_id=ident,
        origin=training.time_range.end,
        time_range={"start": "2026-03-10T00:00:00+03:00", "end": "2026-03-12T00:00:00+03:00"},
        route_ids=training.route_ids,
    )
    result = service.predict(spec)
    _, frame = service.frame(result["id"])
    assert len(frame) == 96
    assert (frame.value > 0).all()  # Route 5 has no unconditional zero policy.
    restarted = ModelService(DatasetRepository(data.root), SnapshotStore(store.root), threads=1)
    manifest, loaded = restarted.load(model["id"])
    from moscowt.modeling.adapters import ADAPTERS

    again = ADAPTERS[adapter].predict(loaded, data.frame(ident), spec)
    np.testing.assert_array_equal(frame.value, again.value)
    assert restarted.predict(spec)["id"] == result["id"]
    assert manifest["spec"]["dataset_id"] == ident
    with pytest.raises(DomainError, match="после момента"):
        restarted.predict(spec.model_copy(update={"origin": pd.Timestamp("2026-02-01", tz="Europe/Moscow")}))
    with pytest.raises(DomainError, match="новых маршрутов"):
        restarted.predict(spec.model_copy(update={"route_ids": ["unknown"]}))


def test_features_cannot_read_future_target(model_data):
    data, ident, _ = model_data
    history = data.frame(ident)
    origin = pd.Timestamp("2026-02-01", tz="Europe/Moscow")
    target = history.loc[history.timestamp >= origin, ["route", "timestamp"]].reset_index(drop=True)
    builder = FeatureBuilder(["5", "new-101"])
    first = builder.build(history, target, origin)
    history.loc[history.timestamp >= origin, "value"] = 1e12
    second = builder.build(history, target, origin)
    pd.testing.assert_frame_equal(first, second)
    assert first.shape[1] == 34
    assert np.isfinite(first.to_numpy()).all()


def test_corrupt_model_rejected(model_data):
    data, ident, store = model_data
    service = ModelService(data, store)
    model = service.train(specs(ident, "seasonal"))
    store.path("trained_models", model["id"], "joblib").write_bytes(b"corrupt")
    with pytest.raises(DomainError, match="Контрольная сумма"):
        service.load(model["id"])
