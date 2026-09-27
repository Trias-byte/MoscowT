"""Fixed-origin exploratory ablations. Every variant shares targets, origins and tree budgets."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.service import ModelService
from moscowt.storage import SnapshotStore, digest, write_json


def metrics(frame):
    error = frame.prediction - frame.truth
    denominator = frame.truth.abs().sum()
    return {
        "wape": float(error.abs().sum() / denominator) if denominator else None,
        "mae": float(error.abs().mean()),
        "bias": float(error.mean()),
        "rows": len(frame),
    }


def main():
    settings = Settings()
    data, store = DatasetRepository(settings.data_root), SnapshotStore(settings.state_dir)
    demo = json.loads((store.root / "demo.json").read_bytes())
    dataset_id = demo["steps"][-1]["dataset_id"]
    external_id = demo["external_id"]
    routes = data.manifest(dataset_id)["routes"]
    service = ModelService(data, store, threads=settings.model_threads)
    # A declared, fixed research budget; these scores are not attributed to the full-size release.
    parameters = {"lgb_estimators": 32, "cat_iterations": 48, "rf_estimators": 32}
    variants = {
        "base": ["calendar"],
        "without_calendar": [],
        "weather": ["calendar", "weather"],
        "events": ["calendar", "events"],
    }
    records = []
    for origin in ("2025-04-01", "2025-07-01", "2025-09-01", "2025-10-01"):
        start = pd.Timestamp(origin, tz="Europe/Moscow")
        end = start + pd.DateOffset(months=1)
        for name, groups in variants.items():
            trained = service.train(
                TrainingSpec(
                    dataset_id=dataset_id,
                    model_type="lgb_cb_rf",
                    route_ids=routes,
                    time_range={"start": "2025-01-01T00:00:00+03:00", "end": start},
                    parameters=parameters,
                    external_snapshot_id=external_id,
                    feature_groups=groups,
                )
            )
            run = service.predict(
                ForecastSpec(
                    model_id=trained["id"],
                    dataset_id=dataset_id,
                    route_ids=routes,
                    origin=start,
                    time_range={"start": start, "end": end},
                )
            )
            _, prediction = service.frame(run["id"])
            truth = data.frame(dataset_id, route_ids=routes, start=start, end=end)
            joined = prediction.merge(
                truth[["route", "timestamp", "value"]],
                on=["route", "timestamp"],
                suffixes=("_prediction", "_truth"),
                validate="one_to_one",
            ).rename(columns={"value_prediction": "prediction", "value_truth": "truth"})
            for horizon, rows in (
                ("day", joined.loc[joined.timestamp < start + pd.Timedelta(days=1)]),
                ("month", joined),
            ):
                records.append(
                    {
                        "origin": origin,
                        "variant": name,
                        "horizon": horizon,
                        "forecast_id": run["id"],
                        **metrics(rows),
                        "route_metrics": {r: metrics(f) for r, f in rows.groupby("route")},
                    }
                )
            print(origin, name, records[-1]["wape"], flush=True)
    effects = {}
    for group, variant, reference in (
        ("weather", "weather", "base"),
        ("events", "events", "base"),
        ("calendar", "base", "without_calendar"),
    ):
        delta = []
        for origin in ("2025-04-01", "2025-07-01", "2025-09-01", "2025-10-01"):

            def find(name):
                return next(
                    r["wape"]
                    for r in records
                    if r["origin"] == origin and r["variant"] == name and r["horizon"] == "month"
                )

            delta.append(find(reference) - find(variant))
        effects[group] = {
            "mean_wape_improvement": float(np.mean(delta)),
            "fold_improvements": delta,
            "status": "improved" if np.mean(delta) > 0 else "no_effect" if np.mean(delta) == 0 else "worse",
            "interpretation": "Exploratory paired ablation; no causal claim and no independent holdout claim",
        }
    effects["traffic"] = {"status": "unavailable", "reason": "Open route-hour traffic archive not confirmed"}
    report = {
        "id": "evaluation-"
        + digest(
            {"dataset": dataset_id, "external": external_id, "parameters": parameters, "records": records}
        ),
        "dataset_id": dataset_id,
        "external_id": external_id,
        "parameters": parameters,
        "records": records,
        "effects": effects,
        "availability_policy": "retrospective_event_time; reanalysis vintage reconstructed later",
        "holdout_policy": "2025 windows have been used in earlier research; genuinely independent validation requires a new incoming period",
    }
    if store.path("evaluations", report["id"]).exists():
        report = store.read("evaluations", report["id"])
    else:
        report["created_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        store.put("evaluations", report["id"], report)
    write_json(Path(__file__).resolve().parents[2] / "docs/source-evaluation.json", report)
    print(json.dumps(effects, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
