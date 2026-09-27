"""Publish a complete model run through the existing map contract without duplicating data."""

import json

from .domain import DomainError
from .storage import digest


def publish_forecast(settings, data, models, request):
    store = models.store
    run, frame = models.frame(request.forecast_id)
    spec = run["spec"]
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
    if not store.current().get("networkId"):
        raise DomainError("NETWORK_NOT_READY", "Сначала подготовьте геометрию для карты", 409)
    fleet_id = store.current().get("fleetId") if store.current().get("historyId") == history_id else None
    if not fleet_id:
        for path in sorted((store.root / "fleets").glob("*.json")):
            fleet = json.loads(path.read_bytes())
            if fleet.get("historyId") == history_id:
                fleet_id = fleet["id"]
                break
    return store.publish(
        expected_snapshot_id=request.expected_snapshot_id,
        historyId=history_id,
        datasetId=history["id"],
        forecastId=run["id"],
        fleetId=fleet_id,
        scheduleId=request.schedule_id,
        scheduleScenario=request.schedule_scenario,
    )
