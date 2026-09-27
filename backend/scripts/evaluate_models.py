"""Quantitative day/month checks of both full service recipes; retrospective, previously used periods."""

import json
from pathlib import Path

import pandas as pd
from evaluate_sources import metrics

from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.service import ModelService
from moscowt.storage import SnapshotStore, digest, write_json

settings = Settings()
data, store = DatasetRepository(settings.data_root), SnapshotStore(settings.state_dir)
demo = json.loads((store.root / "demo.json").read_bytes())
ident = demo["steps"][-1]["dataset_id"]
routes = data.manifest(ident)["routes"]
service = ModelService(data, store, settings.model_threads)
records = []
for origin in ("2025-09-01", "2025-10-01"):
    start = pd.Timestamp(origin, tz="Europe/Moscow")
    end = start + pd.DateOffset(months=1)
    truth = data.frame(ident, route_ids=routes, start=start, end=end)
    for name in ("seasonal", "lgb_cb_rf"):
        trained = service.train(
            TrainingSpec(
                dataset_id=ident,
                model_type=name,
                route_ids=routes,
                time_range={"start": "2025-01-01T00:00:00+03:00", "end": start},
                external_snapshot_id=demo["external_id"],
            )
        )
        run = service.predict(
            ForecastSpec(
                model_id=trained["id"],
                dataset_id=ident,
                route_ids=routes,
                origin=start,
                time_range={"start": start, "end": end},
            )
        )
        _, predicted = service.frame(run["id"])
        frame = predicted.merge(
            truth[["route", "timestamp", "value"]],
            on=["route", "timestamp"],
            suffixes=("_prediction", "_truth"),
        ).rename(columns={"value_prediction": "prediction", "value_truth": "truth"})
        record = {
            "model_type": name,
            "origin": origin,
            "model_id": trained["id"],
            "forecast_id": run["id"],
            "day": metrics(frame.loc[frame.timestamp < start + pd.Timedelta(days=1)]),
            "month_hourly": metrics(frame),
            "route_metrics": {r: metrics(f) for r, f in frame.groupby("route")},
        }
        for grain in ("day", "month"):
            keys = frame.timestamp.dt.strftime("%Y-%m-%d" if grain == "day" else "%Y-%m")
            aggregated = frame.groupby([frame.route, keys])[["prediction", "truth"]].sum()
            record[grain + "_totals"] = metrics(aggregated)
        records.append(record)
        print(name, origin, record["day"]["wape"], record["month_hourly"]["wape"], flush=True)
report = {
    "id": "validation-" + digest(records),
    "dataset_id": ident,
    "records": records,
    "holdout_policy": "Retrospective fixed-origin checks on previously used 2025 periods; no independent holdout claim",
    "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
}
if store.path("reports", report["id"]).exists():
    report = store.read("reports", report["id"])
else:
    store.put("reports", report["id"], report)
write_json(Path(__file__).resolve().parents[2] / "docs/model-validation.json", report)
