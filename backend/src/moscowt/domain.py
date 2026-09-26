from datetime import date as CalendarDate
from datetime import datetime, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROUTES = (1, 5, 7, 11, 12, 17, 25, 26, 28, 50)
TZ = ZoneInfo("Europe/Moscow")
HISTORY_START = datetime(2025, 1, 1, tzinfo=TZ)
HISTORY_END = datetime(2025, 11, 1, tzinfo=TZ)
FINAL_END = datetime(2026, 1, 1, tzinfo=TZ)
METRIC = "successful_validations"


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TimeRange(StrictModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def aware_hour(cls, v):
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("An explicit timezone offset is required")
        v = v.astimezone(TZ)
        if v.minute or v.second or v.microsecond:
            raise ValueError("Time must be aligned to a Moscow calendar hour")
        return v

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Expected a non-empty half-open interval")
        return self


class Section(StrictModel):
    patternId: str
    fromId: str
    toId: str


class GeometryScope(StrictModel):
    date: CalendarDate | None = None
    patternIds: list[str] = Field(default_factory=list, max_length=20)
    section: Section | None = None
    bbox: tuple[float, float, float, float] | None = None
    referenceMode: Literal["reference", "historical"] = "reference"

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, b):
        if b and not (-180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
            raise ValueError("Invalid bounding box")
        return b


class Scope(StrictModel):
    routeIds: list[str] = Field(min_length=1, max_length=10)
    metric: str = METRIC
    metricScope: str = "route"
    mode: Literal["auto", "history", "forecast"] = "auto"
    timeRange: TimeRange
    grain: Literal["hour", "day"] = "hour"
    aggregation: Literal["sum"] = "sum"
    snapshotId: str
    geometry: GeometryScope = Field(default_factory=GeometryScope)

    @model_validator(mode="after")
    def supported(self):
        if len(set(self.routeIds)) != len(self.routeIds):
            raise ValueError("Duplicate route IDs")
        if self.metric != METRIC:
            raise DomainError("METRIC_UNAVAILABLE", "Доступны только успешные валидации")
        if self.metricScope != "route":
            raise DomainError("METRIC_GRAIN_UNAVAILABLE", "Числовой показатель доступен только по маршруту")
        if any(r not in {str(x) for x in ROUTES} for r in self.routeIds):
            raise DomainError("ROUTE_DATA_UNAVAILABLE", "Для выбранного маршрута нет числовых данных")
        if self.grain == "day" and (self.timeRange.start.hour or self.timeRange.end.hour):
            raise ValueError("Daily intervals must be aligned to midnight in Moscow")
        step = timedelta(hours=1) if self.grain == "hour" else timedelta(days=1)
        if (self.timeRange.end - self.timeRange.start) / step > 1464:
            raise DomainError("RANGE_TOO_LARGE", "Допустимо не более 1464 временных интервалов")
        return self


class ExportRequest(StrictModel):
    scope: Scope
    index: int | None = Field(default=None, ge=0)
    format: Literal["csv"] = "csv"


class SubmissionRequest(StrictModel):
    snapshotId: str


class ForecastExpert(Protocol):
    def predict(self, timestamps, routes) -> np.ndarray: ...


def moscow_origin(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
