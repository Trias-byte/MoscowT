from datetime import datetime

import numpy as np
from pydantic import Field, field_validator

from .domain import TZ, DomainError, StrictModel, TimeRange
from .storage import digest


class Coefficients(StrictModel):
    weather: float = Field(default=1, ge=0, le=2)
    event: float = Field(default=1, ge=0, le=2)
    season: float = Field(default=1, ge=0, le=2)

    @field_validator("weather", "event", "season")
    @classmethod
    def slider_step(cls, value):
        if abs(value * 20 - round(value * 20)) > 1e-8:
            raise ValueError("Шаг коэффициента — 0.05")
        return value

    @property
    def multiplier(self):
        return self.weather * self.event * self.season


class ScenarioSpec(StrictModel):
    forecast_id: str
    route_ids: list[str] = Field(min_length=1, max_length=1000)
    time_range: TimeRange
    coefficients: Coefficients = Field(default_factory=Coefficients)
    additional_vehicle_hours: float = Field(default=0, ge=0, le=1e7)


class ScenarioService:
    def __init__(self, store):
        self.store = store

    def create(self, spec):
        run = self.store.read("forecast_runs", spec.forecast_id)
        selected = TimeRange.model_validate(run["spec"]["time_range"])
        if set(spec.route_ids) - set(run["spec"]["route_ids"]) or len(set(spec.route_ids)) != len(
            spec.route_ids
        ):
            raise DomainError("INVALID_SCENARIO_ROUTES", "Маршруты сценария должны входить в прогноз")
        if spec.time_range.start < selected.start or spec.time_range.end > selected.end:
            raise DomainError("INVALID_SCENARIO_RANGE", "Сценарий должен быть внутри периода прогноза")
        value = spec.model_dump(mode="json")
        ident = "scenario-" + digest(value)
        with self.store.lock("scenario"):
            if self.store.path("scenarios", ident).exists():
                return self.store.read("scenarios", ident)
            result = {
                "id": ident,
                "spec": value,
                "created_at": datetime.now(TZ).isoformat(),
                "method": "manual_multiplicative_scenario",
                "note": "Ручной сценарий, не измеренный эффект внешних источников",
            }
            self.store.put("scenarios", ident, result)
            return result

    def apply(self, frame, ident, forecast_id):
        spec = ScenarioSpec.model_validate(self.store.read("scenarios", ident)["spec"])
        if spec.forecast_id != forecast_id:
            raise DomainError("SCENARIO_FORECAST_MISMATCH", "Сценарий относится к другому прогнозу", 409)
        frame = frame.copy()
        frame["base_value"] = frame.value
        mask = (
            frame.route.isin(spec.route_ids)
            & frame.timestamp.ge(spec.time_range.start)
            & frame.timestamp.lt(spec.time_range.end)
        )
        if "provenance" in frame:
            mask &= frame.provenance.eq("forecast")
        frame.loc[mask, "value"] *= spec.coefficients.multiplier
        frame["scenario_id"] = ident
        frame["coefficient"] = np.where(mask, spec.coefficients.multiplier, 1.0)
        frame["additional_vehicle_hours"] = 0.0
        # Allocate the fixed budget equally across the full scenario grid, independent of query slice.
        cells = len(spec.route_ids) * int(
            (spec.time_range.end - spec.time_range.start).total_seconds() / 3600
        )
        frame.loc[mask, "additional_vehicle_hours"] = spec.additional_vehicle_hours / cells
        if "vehicle_hours" in frame:
            frame["base_vehicle_hours"] = frame.vehicle_hours
            frame.loc[mask, "vehicle_hours"] += frame.loc[mask, "additional_vehicle_hours"]
        return frame
