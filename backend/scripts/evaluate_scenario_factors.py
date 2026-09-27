"""Paired ablations of hourly weather/calendar/accidents, with honest fixed-origin and diagnostic modes."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.factors import FactorRepository
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.service import ModelService
from moscowt.storage import SnapshotStore, digest, write_json


def metrics(rows):
    valid = rows.dropna(subset=["truth", "prediction"])
    error = valid.prediction - valid.truth
    denom = valid.truth.abs().sum()
    return {
        "wape": float(error.abs().sum() / denom) if denom else None,
        "mae": float(error.abs().mean()),
        "bias": float(error.mean()),
        "rows": len(valid),
    }


def main():
    settings = Settings()
    data, store = DatasetRepository(settings.data_root), SnapshotStore(settings.state_dir)
    factors = FactorRepository(settings.data_root)
    weather = factors.list("weather_hourly")[-1]["id"]
    accidents = factors.list("accident_links")[-1]["id"]
    dataset = data.catalog.current_id()
    routes = data.manifest(dataset)["routes"]
    service = ModelService(data, store, threads=2)
    parameters = {"cat_iterations": 144, "lgb_estimators": 96, "rf_estimators": 64}
    variants = {
        "full": ["calendar", "weather", "accidents"],
        "without_calendar": ["weather", "accidents"],
        "without_weather": ["calendar", "accidents"],
        "without_accidents": ["calendar", "weather"],
    }
    origins = ["2025-06-01", "2025-09-01", "2025-10-01"]
    records = []
    for origin in origins:
        start = pd.Timestamp(origin, tz="Europe/Moscow")
        end = start + pd.DateOffset(months=1)
        truth = data.frame(dataset, route_ids=routes, start=start, end=end)[
            ["route", "timestamp", "value"]
        ].rename(columns={"value": "truth"})
        for name, groups in variants.items():
            print("Training", origin, name, flush=True)
            model = service.train(
                TrainingSpec(
                    dataset_id=dataset,
                    model_type="lgb_cb_rf",
                    route_ids=routes,
                    time_range={"start": "2025-01-01T00:00:00+03:00", "end": start},
                    parameters=parameters,
                    weather_hourly_id=weather,
                    accident_links_id=accidents,
                    feature_groups=groups,
                    purpose="research",
                )
            )
            for diagnostic in (False, True):
                run = service.predict(
                    ForecastSpec(
                        dataset_id=dataset,
                        model_id=model["id"],
                        route_ids=routes,
                        origin=start,
                        time_range={"start": start, "end": end},
                        diagnostic_observed_factors=diagnostic,
                    )
                )
                forecast = service.frame(run["id"])[1].rename(columns={"value": "prediction"})
                joined = forecast.merge(truth, on=["route", "timestamp"], validate="one_to_one")
                for horizon, rows in [
                    ("day", joined.loc[joined.timestamp < start + pd.Timedelta(days=1)]),
                    ("month", joined),
                ]:
                    daily = (
                        rows.assign(day=rows.timestamp.dt.strftime("%Y-%m-%d"))
                        .groupby(["route", "day"])[["truth", "prediction"]]
                        .sum(min_count=1)
                        .reset_index()
                    )
                    records.append(
                        {
                            "origin": origin,
                            "variant": name,
                            "horizon": horizon,
                            "mode": "retrospective_diagnostic" if diagnostic else "fixed_origin_climatology",
                            "forecast_id": run["id"],
                            **metrics(rows),
                            "daily_totals": metrics(daily),
                            "route_metrics": {r: metrics(f) for r, f in rows.groupby("route")},
                        }
                    )
                print(origin, name, diagnostic, records[-1]["wape"], flush=True)
    effects = {}
    for group in ("calendar", "weather", "accidents"):
        modes = {}
        for mode in ("fixed_origin_climatology", "retrospective_diagnostic"):
            values = []
            for origin in origins:

                def score(variant):
                    return next(
                        r["wape"]
                        for r in records
                        if r["origin"] == origin
                        and r["variant"] == variant
                        and r["mode"] == mode
                        and r["horizon"] == "month"
                    )

                values.append(score("without_" + group) - score("full"))
            mean = float(np.mean(values))
            modes[mode] = {
                "fold_improvements": values,
                "mean_wape_improvement": mean,
                "min": min(values),
                "max": max(values),
                "std": float(np.std(values)),
                "status": "improved" if mean > 0 else "worse" if mean < 0 else "no_effect",
            }
        effects[group] = {"status": modes["fixed_origin_climatology"]["status"], "modes": modes}
    effects["schedule"] = {
        "status": "unconfirmed",
        "reason": "No independent complete timetable route-days in 2025; validation-derived vehicle activity is endogenous, not a training source",
        "elasticity": 0.3,
        "method": "user_scenario_assumption",
    }
    effects["traffic"] = {
        "status": "unavailable",
        "reason": "Accidents are not a speed/congestion time series",
    }
    report = {
        "id": "evaluation-" + digest({"records": records, "parameters": parameters}),
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "dataset_id": dataset,
        "weather_hourly_id": weather,
        "accident_links_id": accidents,
        "parameters": parameters,
        "origins": origins,
        "records": records,
        "effects": effects,
        "holdout_policy": "Exploratory already-used 2025 periods; not an independent holdout",
        "weather_policy": "Observed historical inputs for fitting; completed prior year for operational inference; actual target weather only in labelled diagnostics",
        "accident_policy": "No archive publication timestamps; future incidents absent operationally; actual target events only in diagnostics; spatial proximity is not proof of tram obstruction",
    }
    store.put("evaluations", report["id"], report)
    write_json(Path("docs/scenario-factor-evaluation.json"), report)
    print(json.dumps(effects, ensure_ascii=False, indent=2), flush=True)
    print("Training service demo", flush=True)
    model = service.train(
        TrainingSpec(
            dataset_id=dataset,
            model_type="lgb_cb_rf",
            route_ids=routes,
            time_range={"start": "2025-01-01T00:00:00+03:00", "end": "2025-11-01T00:00:00+03:00"},
            parameters=parameters,
            weather_hourly_id=weather,
            accident_links_id=accidents,
            feature_groups=variants["full"],
        )
    )
    run = service.predict(
        ForecastSpec(
            dataset_id=dataset,
            model_id=model["id"],
            route_ids=routes,
            origin="2025-11-01T00:00:00+03:00",
            time_range={"start": "2025-11-01T00:00:00+03:00", "end": "2026-01-01T00:00:00+03:00"},
        )
    )
    write_json(
        Path("docs/scenario-demo.json"),
        {
            "model_id": model["id"],
            "forecast_id": run["id"],
            "dataset_id": dataset,
            "parameters": parameters,
            "evaluation_id": report["id"],
        },
    )
    print("Demo ready", run["id"], flush=True)


if __name__ == "__main__":
    main()
