from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..domain import StrictModel, TimeRange


class TrainingSpec(StrictModel):
    dataset_id: str
    model_type: Literal["seasonal", "lgb_cb_rf", "annual_scenario"] = "seasonal"
    route_ids: list[str] = Field(min_length=1, max_length=1000)
    time_range: TimeRange
    parameters: dict[str, Any] = Field(default_factory=dict)
    external_snapshot_id: str | None = None
    feature_groups: list[Literal["calendar", "weather", "events"]] = Field(
        default_factory=lambda: ["calendar"], max_length=3
    )
    seed: int = Field(default=42, ge=0, le=2**31 - 1)

    @field_validator("route_ids")
    @classmethod
    def unique_routes(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate route IDs")
        return value


class ForecastSpec(StrictModel):
    model_id: str
    dataset_id: str
    origin: datetime
    time_range: TimeRange
    route_ids: list[str] = Field(min_length=1, max_length=1000)
    weather_forecast_id: str | None = None

    _hour = field_validator("origin")(TimeRange.aware_hour.__func__)
    _routes = field_validator("route_ids")(TrainingSpec.unique_routes.__func__)

    @model_validator(mode="after")
    def after_origin(self):
        if self.time_range.start < self.origin:
            raise ValueError("Forecast starts before origin")
        return self


class ModelAdapter(ABC):
    """Adapters own the recipe; the service owns validation and artifact publication."""

    name: str
    version: str
    minimum_days: int

    def capabilities(self):
        return {
            "id": self.name,
            "recipe_version": self.version,
            "minimum_history_days": self.minimum_days,
            "route_policy": "retrain_to_add_route",
            "maximum_horizon_days": 366 if self.name == "annual_scenario" else 62,
            "quality_status": "unvalidated_scenario"
            if self.name == "annual_scenario"
            else "see_fixed_origin_evaluation",
            "availability_policy": "retrospective_event_time",
        }

    @abstractmethod
    def fit(self, history, spec: TrainingSpec, threads: int): ...

    @abstractmethod
    def predict(self, model, history, spec: ForecastSpec): ...
