"""Publish a complete model run through the existing map contract without duplicating data."""

import json
from functools import lru_cache
from pathlib import Path

from .domain import DomainError
from .storage import digest


def is_research(store, run):
    model = store.read("trained_models", run["spec"]["model_id"])
    return run.get("purpose") == "research" or model["spec"].get("purpose") == "research"


def publish_forecast(settings, data, models, request, *, base_snapshot=None, activate=True):
    store = models.store
    current = base_snapshot or store.current()
    run, frame = models.frame(request.forecast_id)
    spec = run["spec"]
    if activate and is_research(store, run):
        raise DomainError(
            "RESEARCH_ONLY",
            "Исследовательский выпуск доступен только в локальном представлении сценария",
            409,
        )
    if spec.get("diagnostic_observed_factors"):
        raise DomainError(
            "DIAGNOSTIC_ONLY", "Ретроспективную диагностику нельзя публиковать как оперативный прогноз"
        )
    data.verify(spec["dataset_id"])
    history = data.manifest(spec["dataset_id"])
    history_id = "history-" + digest({"dataset": history["id"], "adapter": 1})
    store.put(
        "histories",
        history_id,
        {
            "id": history_id,
            "datasetId": history["id"],
            "start": history["start"],
            "end": history["end"],
            "routes": history["routes"],
            "metric": history["metric"],
            "rows": history["rows"],
            "coverage": "provided_extract",
        },
    )
    store.put(
        "forecasts",
        run["id"],
        {
            "id": run["id"],
            "forecastRunId": run["id"],
            "datasetId": history["id"],
            "historyId": history_id,
            "modelId": spec["model_id"],
            "start": spec["time_range"]["start"],
            "end": spec["time_range"]["end"],
            "issuedAt": spec["origin"],
            "createdAt": run["created_at"],
            "horizon": "custom",
            "routes": spec["route_ids"],
            "rows": len(frame),
        },
    )
    if request.schedule_id:
        from .schedules import ScheduleService

        ScheduleService(settings.data_root).store.read("schedules", request.schedule_id)
    if not current.get("networkId"):
        raise DomainError("NETWORK_NOT_READY", "Сначала подготовьте геометрию для карты", 409)
    fleet_id = current.get("fleetId") if current.get("historyId") == history_id else None
    if not fleet_id:
        for path in sorted((store.root / "fleets").glob("*.json")):
            fleet = json.loads(path.read_bytes())
            if fleet.get("historyId") == history_id:
                fleet_id = fleet["id"]
                break
    changes = dict(
        historyId=history_id,
        datasetId=history["id"],
        forecastId=run["id"],
        fleetId=fleet_id,
        scheduleId=request.schedule_id,
        scheduleScenario=request.schedule_scenario,
    )
    if activate:
        return store.publish(expected_snapshot_id=request.expected_snapshot_id, **changes)
    return store.create_snapshot(current, **changes)


@lru_cache(maxsize=16)
def annual_options(root, dataset_id, origin, routes, revision):
    """Ready annual runs for the same data/origin; directory mtime invalidates discovery."""
    result = []
    for path in sorted((Path(root) / "forecast_runs").glob("*.json")):
        run = json.loads(path.read_bytes())
        spec = run["spec"]
        if (
            run.get("model_type") in ("annual_scenario", "lgb_cb_rf")
            and spec["dataset_id"] == dataset_id
            and spec["origin"] == origin
            and set(routes).issubset(spec["route_ids"])
            and not spec.get("diagnostic_observed_factors")
            and run.get("purpose", "service") == "service"
        ):
            result.append(
                {
                    "id": run["id"],
                    **spec["time_range"],
                    "origin": origin,
                    "kind": "annual_scenario" if run.get("model_type") == "annual_scenario" else "primary",
                    "qualityNote": run.get("quality_note", "Невалидированный годовой сценарий"),
                }
            )
    return sorted(result, key=lambda r: (r["kind"] == "annual_scenario", r["end"], r["id"]))
