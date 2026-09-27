import calendar
import tempfile
from datetime import datetime, timedelta

import catboost
import numpy as np
import pandas as pd

from .constants.forecasting import (
    RECIPE as RECIPE,
)
from .domain import HISTORY_END, HISTORY_START, ROUTES, TZ, DomainError
from .pipelines import parquet_write
from .storage import SnapshotStore, atomic_write, digest, file_hash, write_json


def features(frame: pd.DataFrame) -> pd.DataFrame:
    t = frame.timestamp.dt
    return pd.DataFrame(
        {
            "route": frame.route.astype(str),
            "hour": t.hour.astype(str),
            "weekday": t.dayofweek.astype(str),
            "month": t.month.astype(str),
            "day": t.day,
            "weekend": (t.dayofweek >= 5).astype(int),
            "elapsedDay": (frame.timestamp - pd.Timestamp(HISTORY_START)).dt.days,
        }
    )


def target_grid(origin: datetime, days: int):
    hours = pd.date_range(origin, origin + timedelta(days=days), inclusive="left", freq="h")
    return pd.MultiIndex.from_product([ROUTES, hours], names=["route", "timestamp"]).to_frame(index=False)


def seasonal_profile(history: pd.DataFrame, origin: datetime):
    prior = history[(history.timestamp < origin) & (history.timestamp >= origin - timedelta(weeks=8))]
    keys = [prior.route, prior.timestamp.dt.dayofweek, prior.timestamp.dt.hour]
    grouped = prior.groupby(keys).value.agg(["mean", "count"])
    return {f"{r}:{d}:{h}": [float(row["mean"]), int(row["count"])] for (r, d, h), row in grouped.iterrows()}


def profile_prediction(grid: pd.DataFrame, profile):
    values, counts = [], []
    for route, stamp in zip(grid.route, grid.timestamp):
        value, count = profile.get(f"{route}:{stamp.dayofweek}:{stamp.hour}", [None, 0])
        values.append(np.nan if value is None else value)
        counts.append(count)
    return np.array(values), np.array(counts)


def fit_model(store: SnapshotStore, history_id: str, origin: datetime, expert: str):
    if origin.hour or origin.minute or origin.second or not HISTORY_START < origin <= HISTORY_END:
        raise ValueError("Training origin must be a Moscow midnight within available history")
    if expert not in ("seasonal", "catboost"):
        raise ValueError("Unknown expert")
    history = pd.read_parquet(store.path("histories", history_id, "parquet"))
    train = history[history.timestamp < origin].reset_index(drop=True)
    if train.empty or train.timestamp.max() + timedelta(hours=1) != origin:
        raise ValueError("Training history does not reach the requested origin")
    profile = seasonal_profile(train, origin)
    definition = {
        "historyId": history_id,
        "origin": origin.isoformat(),
        "expert": expert,
        "recipe": RECIPE,
        "catboostVersion": catboost.__version__,
    }
    ident = "model-" + digest(definition)
    if store.path("models", ident).exists():
        return store.read("models", ident)
    model_hash = None
    if expert == "catboost":
        model = catboost.CatBoostRegressor(
            **{k: v for k, v in RECIPE.items() if k != "schema"}, verbose=False, allow_writing_files=False
        )
        model.fit(features(train), train.value, cat_features=["route", "hour", "weekday", "month"])
        with tempfile.TemporaryDirectory() as temp:
            from pathlib import Path

            path = Path(temp) / "model.cbm"
            model.save_model(str(path))
            atomic_write(store.path("models", ident, "cbm"), path.read_bytes())
        model_hash = file_hash(store.path("models", ident, "cbm"))
    artifact = {
        "id": ident,
        **definition,
        "trainingStart": HISTORY_START.isoformat(),
        "trainingEnd": origin.isoformat(),
        "featureCutoff": origin.isoformat(),
        "featureSchema": list(features(train.iloc[:1]).columns),
        "modelHash": model_hash,
        "metric": "successful_validations",
        "supportedHorizons": ["day", "month", "competition_61d"],
        "availabilityPolicy": "retrospective_event_time",
        "profile": profile,
        "route5Policy": "zero_fallback_no_positive_history",
    }
    store.put("models", ident, artifact)
    return artifact


def predict(store: SnapshotStore, artifact, origin: datetime, days: int):
    if origin != datetime.fromisoformat(artifact["origin"]) or days not in range(1, 62):
        raise DomainError(
            "MODEL_CUTOFF_MISMATCH", "Модель должна соответствовать моменту выпуска; горизонт 1–61 день"
        )
    grid = target_grid(origin, days)
    baseline, counts = profile_prediction(grid, artifact["profile"])
    if not np.isfinite(baseline).all():
        raise DomainError("INSUFFICIENT_HISTORY", "Недостаточно истории для сезонного профиля")
    if artifact["expert"] == "catboost":
        path = store.path("models", artifact["id"], "cbm")
        if file_hash(path) != artifact["modelHash"]:
            raise ValueError("Model checksum mismatch")
        model = catboost.CatBoostRegressor(thread_count=2)
        model.load_model(str(path))
        values = model.predict(features(grid), thread_count=2)
    else:
        values = baseline.copy()
    grid["value"] = np.maximum(values, 0)
    grid.loc[grid.route == 5, "value"] = 0.0
    if not np.isfinite(grid.value).all():
        raise ValueError("Non-finite prediction")
    grid["baseline"], grid["baselineCount"] = baseline, counts
    grid["valueOrigin"] = "model"
    grid.loc[grid.route == 5, "valueOrigin"] = "zero_fallback_no_positive_history"
    grid["coverageStatus"] = "forecast"
    grid["qualityFlag"] = "none"
    return grid


def issue_forecast(store: SnapshotStore, model_id: str, horizon="competition_61d", publish=True):
    artifact = store.read("models", model_id)
    origin = datetime.fromisoformat(artifact["origin"])
    days = {"day": 1, "month": calendar.monthrange(origin.year, origin.month)[1], "competition_61d": 61}[
        horizon
    ]
    if horizon == "month" and origin.day != 1:
        raise ValueError("Monthly forecast must start on the first day")
    ident = "forecast-" + digest({"model": model_id, "horizon": horizon})
    if not store.path("forecasts", ident).exists():
        frame = predict(store, artifact, origin, days)
        parquet_write(store.path("forecasts", ident, "parquet"), frame)
        manifest = {
            "id": ident,
            "modelId": model_id,
            "historyId": artifact["historyId"],
            "origin": origin.isoformat(),
            "issuedAt": origin.isoformat(),
            "createdAt": datetime.now(TZ).isoformat(),
            "horizon": horizon,
            "start": origin.isoformat(),
            "end": (origin + timedelta(days=days)).isoformat(),
            "rows": len(frame),
            "expert": artifact["expert"],
            "trainingEnd": artifact["trainingEnd"],
            "availabilityPolicy": artifact["availabilityPolicy"],
            "sha256": file_hash(store.path("forecasts", ident, "parquet")),
        }
        store.put("forecasts", ident, manifest)
    manifest = store.read("forecasts", ident)
    if publish:
        store.publish(historyId=artifact["historyId"], forecastId=ident)
    return manifest


def evaluate(actual, predicted):
    truth = np.asarray(actual, dtype=float)
    pred = np.asarray(predicted, dtype=float)
    if truth.shape != pred.shape or not np.isfinite(truth).all() or not np.isfinite(pred).all():
        raise ValueError("Invalid scoring arrays")
    error, total = float(np.abs(truth - pred).sum()), float(truth.sum())
    wape = error / total if total else None
    return {
        "absoluteError": error,
        "actualSum": total,
        "wape": wape,
        "score": max(0, 1 - wape) if wape is not None else None,
    }


def backtest(store: SnapshotStore, history_id: str):
    history = pd.read_parquet(store.path("histories", history_id, "parquet"))
    folds = []
    for origin in [datetime(2025, 5, 1, tzinfo=TZ), datetime(2025, 7, 1, tzinfo=TZ)]:
        for expert in ("seasonal", "catboost"):
            artifact = fit_model(store, history_id, origin, expert)
            prediction = predict(store, artifact, origin, 61)
            truth = history[(history.timestamp >= origin) & (history.timestamp < origin + timedelta(days=61))]
            joined = prediction.merge(
                truth[["route", "timestamp", "value"]],
                on=["route", "timestamp"],
                suffixes=("_pred", "_true"),
                validate="one_to_one",
            )
            if len(joined) != 14640:
                raise ValueError("Incomplete backtest grid")
            folds.append(
                {
                    "origin": origin.isoformat(),
                    "expert": expert,
                    **evaluate(joined.value_true, joined.value_pred),
                }
            )
    selection = {}
    for expert in ("seasonal", "catboost"):
        selected = [r for r in folds if r["expert"] == expert]
        selection[expert] = sum(r["absoluteError"] for r in selected) / sum(r["actualSum"] for r in selected)
    chosen = min(selection, key=selection.get)
    origin = datetime(2025, 9, 1, tzinfo=TZ)
    artifact = fit_model(store, history_id, origin, chosen)
    heldout = predict(store, artifact, origin, 61)
    truth = history[history.timestamp >= origin]
    joined = heldout.merge(
        truth[["route", "timestamp", "value"]],
        on=["route", "timestamp"],
        suffixes=("_pred", "_true"),
        validate="one_to_one",
    )
    if len(joined) != 14640:
        raise ValueError("Incomplete holdout grid")
    report = {
        "historyId": history_id,
        "selectionFolds": folds,
        "selectionWape": selection,
        "selectedExpert": chosen,
        "selectionRule": "minimum pooled early-fold WAPE; seasonal wins ties",
        "holdout": {
            "origin": origin.isoformat(),
            "modelId": artifact["id"],
            **evaluate(joined.value_true, joined.value_pred),
        },
        "holdoutByRoute": {str(r): evaluate(g.value_true, g.value_pred) for r, g in joined.groupby("route")},
        "availabilityPolicy": "retrospective_event_time",
        "recipe": RECIPE,
    }
    report_id = "backtest-" + digest(
        {"historyId": history_id, "recipe": RECIPE, "version": catboost.__version__}
    )
    store.put("reports", report_id, report)
    write_json(store.root / "backtest.json", report)
    issue_forecast(store, artifact["id"], publish=False)
    return report
