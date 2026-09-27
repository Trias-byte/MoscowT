from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field, field_validator, model_validator

from .domain import TZ, DomainError, StrictModel, TimeRange
from .storage import digest, file_hash


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
    engine: Literal["legacy", "recompute"] = "legacy"
    name: str = Field(default="Сценарий", min_length=1, max_length=160)
    weather: "WeatherChanges" = Field(default_factory=lambda: WeatherChanges())
    incidents: list["Incident"] = Field(default_factory=list, max_length=100)
    schedule: "ScheduleChanges" = Field(default_factory=lambda: ScheduleChanges())


class WeatherChanges(StrictModel):
    temperature_2m: float | None = Field(default=None, ge=-60, le=60)
    relative_humidity_2m: float | None = Field(default=None, ge=0, le=100)
    precipitation: float | None = Field(default=None, ge=0, le=200)


class Incident(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    longitude: float = Field(ge=36.5, le=38.3)
    latitude: float = Field(ge=54.9, le=56.2)
    route_ids: list[str] = Field(min_length=1, max_length=1000)
    start: datetime
    duration_minutes: int = Field(default=60, ge=1, le=10080)
    reduction: float = Field(default=0.5, ge=0, le=1)

    @field_validator("start")
    @classmethod
    def aware(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Время ДТП должно включать часовой пояс")
        return value.astimezone(TZ)


class DepartureEdit(StrictModel):
    route_id: str
    timestamp: datetime
    change: Literal[-1, 1] = 1
    duration_minutes: int = Field(default=60, ge=1, le=1440)

    _aware = field_validator("timestamp")(Incident.aware.__func__)


class ScheduleChanges(StrictModel):
    base_schedule_id: str | None = None
    schedule_id: str | None = None
    allow_period_reuse: bool = False
    base_headway_minutes: float | None = Field(default=None, gt=0, le=240)
    headway_minutes: float | None = Field(default=None, gt=0, le=240)
    service_start_minute: int | None = Field(default=None, ge=0, le=1439)
    service_end_minute: int | None = Field(default=None, ge=0, le=1439)
    elasticity: float = Field(default=0.3, ge=0, le=1)
    departures: list[DepartureEdit] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def hours_pair(self):
        if (self.service_start_minute is None) != (self.service_end_minute is None):
            raise ValueError("Укажите обе границы часов работы")
        if self.schedule_id and self.headway_minutes is not None:
            raise ValueError("Выберите альтернативное расписание или новый интервал")
        return self


ScenarioSpec.model_rebuild()


class ScenarioService:
    def __init__(self, store):
        self.store = store

    def create(self, spec):
        if spec.engine == "legacy" and (
            spec.weather.model_dump(exclude_none=True) or spec.incidents or spec.schedule != ScheduleChanges()
        ):
            raise DomainError("SCENARIO_ENGINE_REQUIRED", "Погода, ДТП и расписание требуют engine=recompute")
        run = self.store.read("forecast_runs", spec.forecast_id)
        selected = TimeRange.model_validate(run["spec"]["time_range"])
        if set(spec.route_ids) - set(run["spec"]["route_ids"]) or len(set(spec.route_ids)) != len(
            spec.route_ids
        ):
            raise DomainError("INVALID_SCENARIO_ROUTES", "Маршруты сценария должны входить в прогноз")
        if spec.time_range.start < selected.start or spec.time_range.end > selected.end:
            raise DomainError("INVALID_SCENARIO_RANGE", "Сценарий должен быть внутри периода прогноза")
        for incident in spec.incidents:
            if set(incident.route_ids) - set(spec.route_ids):
                raise DomainError("INVALID_INCIDENT_ROUTES", "ДТП затрагивает маршруты вне сценария")
        if len({i.id for i in spec.incidents}) != len(spec.incidents):
            raise DomainError("DUPLICATE_INCIDENT", "Идентификаторы ДТП повторяются")
        for departure in spec.schedule.departures:
            if (
                departure.route_id not in spec.route_ids
                or not spec.time_range.start <= departure.timestamp < spec.time_range.end
            ):
                raise DomainError("INVALID_DEPARTURE", "Отправление выходит за область сценария")
        value = spec.model_dump(mode="json")
        ident = "scenario-" + digest(value)
        with self.store.lock("scenario"):
            if self.store.path("scenarios", ident).exists():
                return self.store.read("scenarios", ident)
            result = {
                "id": ident,
                "spec": value,
                "created_at": datetime.now(TZ).isoformat(),
                "method": "draft_recompute"
                if spec.engine == "recompute"
                else "manual_multiplicative_scenario",
                "note": "Ручной сценарий, не измеренный эффект внешних источников",
            }
            self.store.put("scenarios", ident, result)
            return result

    def apply(self, frame, ident, forecast_id):
        record = self.store.read("scenarios", ident)
        spec = ScenarioSpec.model_validate(record["spec"])
        if spec.forecast_id != forecast_id:
            raise DomainError("SCENARIO_FORECAST_MISMATCH", "Сценарий относится к другому прогнозу", 409)
        if spec.engine == "recompute":
            if not record.get("result_file"):
                raise DomainError("SCENARIO_NOT_READY", "Сначала рассчитайте сценарий", 409)
            from .tasks import WorkQueue

            if WorkQueue(self.store.root).get(record["job_id"])["status"] != "ready":
                raise DomainError("SCENARIO_NOT_READY", "Расчёт сценария не завершён", 409)
            path = self.store.path("scenario_results", record["result_file"], "parquet")
            if file_hash(path) != record["sha256"]:
                raise DomainError("SCENARIO_CORRUPTED", "Контрольная сумма сценария не совпадает")
            result = pd.read_parquet(path)
            original = frame.copy()
            columns = [
                "value",
                "base_value",
                "vehicle_hours",
                "base_vehicle_hours",
                "additional_vehicle_hours",
                "fleet_method",
            ]
            joined = original[["route", "timestamp"]].merge(
                result[["route", "timestamp", *columns]],
                on=["route", "timestamp"],
                how="left",
                validate="one_to_one",
            )
            selected = joined.value.notna()
            if "provenance" in original:
                selected &= original.provenance.eq("forecast").to_numpy()
            for col in columns:
                if col not in original:
                    original[col] = np.nan if col != "fleet_method" else "missing"
                original.loc[selected.to_numpy(), col] = joined.loc[selected, col].to_numpy()
            original["scenario_id"] = ident
            return original
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
