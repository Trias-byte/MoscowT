"""Source-grounded scenario hints; object identity and query scope are checked first."""

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from .domain import DomainError, StrictModel, TimeRange
from .modeling.contracts import ForecastSpec


class ContextObject(StrictModel):
    kind: Literal["route", "stop", "segment"]
    id: str


class FactorContextRequest(StrictModel):
    route_ids: list[str] = Field(min_length=1, max_length=1000)
    time_range: TimeRange
    snapshot_id: str | None = None
    object: ContextObject | None = None
    schedule_id: str | None = None
    allow_period_reuse: bool = False


def describe_series(values):
    values = pd.to_numeric(pd.Series(values), errors="coerce")
    known = values.loc[np.isfinite(values)]
    return {
        "min": float(known.min()) if len(known) else None,
        "max": float(known.max()) if len(known) else None,
        "mean": float(known.mean()) if len(known) else None,
        "known_hours": len(known),
        "total_hours": len(values),
    }


def weather_profile(settings, store, ident, period):
    from .factors import hourly_weather

    run = store.read("forecast_runs", ident)
    spec = ForecastSpec.model_validate(run["spec"])
    model = store.read("trained_models", spec.model_id)
    source_id = model["spec"].get("weather_hourly_id")
    if not source_id or "weather" not in model["spec"].get("feature_groups", []):
        raise DomainError("WEATHER_UNSUPPORTED", "У модели нет почасовых погодных признаков")
    if period.start < spec.time_range.start or period.end > spec.time_range.end:
        raise DomainError("INVALID_SCENARIO_RANGE", "Период должен быть внутри выпуска")
    target = pd.DataFrame({"timestamp": pd.date_range(period.start, period.end, freq="h", inclusive="left")})
    profile = hourly_weather(
        str(settings.data_root),
        source_id,
        target,
        spec.origin,
        spec.diagnostic_observed_factors,
        spec.weather_forecast_id,
    )
    method = "retrospective_actual" if spec.diagnostic_observed_factors else "prior_year_climatology"
    if spec.weather_forecast_id:
        method = "received_forecast_then_climatology"
    return {
        "forecast_id": ident,
        "source_id": spec.weather_forecast_id or source_id,
        "method": method,
        "fields": {key: describe_series(profile[key]) for key in profile.columns},
    }


def factor_context(settings, store, ident, request):
    from .network_history import resolve_network
    from .scenario_engine import timetable_departures
    from .schedules import ScheduleService

    run = store.read("forecast_runs", ident)
    spec = ForecastSpec.model_validate(run["spec"])
    if len(set(request.route_ids)) != len(request.route_ids) or set(request.route_ids) - set(spec.route_ids):
        raise DomainError("INVALID_FACTOR_SCOPE", "Маршруты подсказки должны входить в выбранный выпуск")
    if request.time_range.start < spec.time_range.start or request.time_range.end > spec.time_range.end:
        raise DomainError("INVALID_SCENARIO_RANGE", "Период подсказки должен быть внутри выпуска")
    routes = request.route_ids
    selected = request.object
    if selected:
        if selected.kind == "route":
            route = selected.id
        else:
            if not request.snapshot_id:
                raise DomainError("GEOMETRY_REQUIRED", "Для подсказки объекта нужен снимок геометрии")
            snapshot = store.read("snapshots", request.snapshot_id)
            network = resolve_network(
                store, store.read("networks", snapshot["networkId"]), str(request.time_range.start.date())
            )
            collection = "stops" if selected.kind == "stop" else "segments"
            item = next((item for item in network[collection] if item["id"] == selected.id), None)
            if item is None:
                raise DomainError(
                    "INVALID_FACTOR_OBJECT", "Объект отсутствует в геометрии выбранного периода"
                )
            route = item["routeId"]
        if route not in routes:
            raise DomainError("INVALID_FACTOR_SCOPE", "Выбранный объект не относится к маршрутам сценария")
        routes = [route]
    try:
        weather = weather_profile(settings, store, ident, request.time_range)
    except DomainError as error:
        if error.code != "WEATHER_UNSUPPORTED":
            raise
        weather = {"method": "unsupported", "fields": {}, "reason": str(error)}
    schedule = {"source_id": request.schedule_id, "routes": [], "method": "unavailable"}
    if request.schedule_id:
        service = ScheduleService(settings.data_root)
        rows = timetable_departures(
            service, request.schedule_id, request.time_range, routes, request.allow_period_reuse
        )
        schedule = {
            "source_id": request.schedule_id,
            "method": "weekly_profile_reuse" if request.allow_period_reuse else "source_schedule",
            "routes": [
                {
                    "route_id": route,
                    "departures": describe_series(group.departures),
                    "vehicle_hours": describe_series(group.vehicle_hours),
                }
                for route, group in rows.groupby("route")
            ],
        }
    return {
        "forecast_id": ident,
        "route_ids": routes,
        "time_range": request.time_range.model_dump(mode="json"),
        "object": selected.model_dump() if selected else None,
        "resolution": "route",
        "note": "Условия относятся к маршруту и периоду. Точность внешних факторов до остановки или участка не подтверждена.",
        "weather": weather,
        "schedule": schedule,
    }
