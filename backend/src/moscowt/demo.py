"""Real-data demo with held-out route and monthly ingestion; never fabricates demand."""

from pathlib import Path

from .data.repository import DatasetRepository
from .data.schemas import ImportSpec
from .external import ExternalRepository
from .modeling.contracts import ForecastSpec, TrainingSpec
from .modeling.service import ModelService
from .pipelines import prepare_network
from .platform_api import PublishRequest
from .publication import publish_forecast
from .schedules import ScheduleService
from .storage import SnapshotStore, write_json


def prepare_demo(settings, weather_dir=None, schedule=None, quick=False):
    store, data = SnapshotStore(settings.state_dir), DatasetRepository(settings.data_root)
    sources = [
        settings.input_path("labels/labels_day_train.csv"),
        settings.input_path("labels/labels_day_test.csv"),
    ]
    routes = ["1", "5", "7", "11", "12", "17", "25", "26", "28", "50"]
    initial_routes = [route for route in routes if route != "17"]
    steps = []
    for name, selected, start, end in (
        ("initial_without_17", initial_routes, "2025-01-01", "2025-09-01"),
        ("add_september", initial_routes, "2025-09-01", "2025-10-01"),
        ("add_real_route_17", ["17"], "2025-01-01", "2025-10-01"),
        ("add_october_truth", routes, "2025-10-01", "2025-11-01"),
    ):
        spec = ImportSpec(
            route_ids=selected,
            time_range={"start": start + "T00:00:00+03:00", "end": end + "T00:00:00+03:00"},
            complete=True,
        )
        report = data.preview(sources, spec)
        published = data.apply(report["id"])
        steps.append({"step": name, "upload_id": report["id"], **published})
    ident = steps[-1]["dataset_id"]
    external = ExternalRepository(settings.data_root).register(Path(weather_dir)) if weather_dir else None
    schedule_ref = ScheduleService(settings.data_root).import_file(Path(schedule)) if schedule else None
    prepare_network(store, settings.dataset_dir)
    service = ModelService(data, store, settings.model_threads, settings.max_forecast_hours)
    models, forecasts = [], []
    parameters = {"lgb_estimators": 32, "cat_iterations": 48, "rf_estimators": 32} if quick else {}
    for adapter in ("lgb_cb_rf", "seasonal", "annual_scenario"):
        model = service.train(
            TrainingSpec(
                dataset_id=ident,
                model_type=adapter,
                route_ids=routes,
                time_range={"start": "2025-01-01T00:00:00+03:00", "end": "2025-11-01T00:00:00+03:00"},
                parameters=parameters if adapter == "lgb_cb_rf" else {},
                external_snapshot_id=external["id"] if external else None,
            )
        )
        models.append(model["id"])
        end = "2026-11-01" if adapter == "annual_scenario" else "2026-01-01"
        run = service.predict(
            ForecastSpec(
                model_id=model["id"],
                dataset_id=ident,
                origin="2025-11-01T00:00:00+03:00",
                time_range={"start": "2025-11-01T00:00:00+03:00", "end": end + "T00:00:00+03:00"},
                route_ids=routes,
            )
        )
        forecasts.append(run["id"])
    current = publish_forecast(settings, data, service, PublishRequest(forecast_id=forecasts[0]))
    from .fleet import prepare_fleet

    prepare_fleet(store, settings.dataset_dir)
    current = store.current()
    result = {
        "steps": steps,
        "models": models,
        "forecasts": forecasts,
        "snapshot": current,
        "external_id": external["id"] if external else None,
        "schedule_id": schedule_ref["id"] if schedule_ref else None,
        "quick_parameters": parameters,
        "data_policy": "Verified competition extract: absent label hours filled with zero; not a claim of all passenger activity",
    }
    write_json(store.root / "demo.json", result)
    return result
