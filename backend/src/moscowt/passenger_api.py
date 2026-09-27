"""Passenger API: fixed research summaries and snapshot-compatible observations."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import Field, model_validator

from .domain import GeometryScope, StrictModel, TimeRange
from .passenger_contracts import (
    PassengerCohorts,
    PassengerInfrastructure,
    PassengerMetadata,
    PassengerPoiCollection,
    PassengerResult,
    PassengerSeasonality,
    PassengerUnavailable,
)
from .passenger_package import CATEGORIES
from .passengers import PassengerRepository


class PassengerQuery(StrictModel):
    snapshot_id: str
    route_ids: list[str] = Field(default_factory=list, max_length=10)
    time_range: TimeRange
    grain: Literal["hour", "day"] = "hour"
    mode: Literal["auto", "history", "forecast"] = "auto"
    categories: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def limits(self):
        if len(set(self.route_ids)) != len(self.route_ids) or len(set(self.categories)) != len(
            self.categories
        ):
            raise ValueError("Повтор фильтра")
        if set(self.categories) - set(CATEGORIES):
            raise ValueError("Неизвестная категория")
        hours = (self.time_range.end - self.time_range.start).total_seconds() / 3600
        if hours > (1464 if self.grain == "hour" else 366 * 24):
            raise ValueError("Слишком большой период")
        if self.grain == "day" and (self.time_range.start.hour or self.time_range.end.hour):
            raise ValueError("Дневной период должен начинаться и заканчиваться в полночь")
        return self


def router(settings, store, data):
    api = APIRouter(prefix="/api/v2/passengers", tags=["passengers"])
    repo = PassengerRepository(settings.data_root, store, data)

    @api.get("/metadata", response_model=PassengerMetadata | PassengerUnavailable)
    def metadata():
        return repo.metadata()

    @api.post("/query", response_model=PassengerResult)
    def query(spec: PassengerQuery):
        return repo.query(spec)

    @api.get("/seasonality", response_model=PassengerSeasonality)
    def seasonality(route_ids: list[str] = Query(default=[], max_length=10)):
        return repo.seasonality(route_ids)

    @api.get("/cohorts", response_model=PassengerCohorts)
    def cohorts():
        return repo.cohorts()

    @api.get("/infrastructure", response_model=PassengerInfrastructure, response_model_exclude_unset=True)
    def infrastructure(
        snapshot_id: str,
        date: date,
        route: str,
        radius: Literal["500", "1000"] = "500",
        stop_id: str | None = None,
    ):
        return repo.infrastructure(snapshot_id, date, route, int(radius), stop_id)

    @api.get("/poi", response_model=PassengerPoiCollection, response_model_exclude_unset=True)
    def poi(
        snapshot_id: str,
        date: date,
        route_ids: list[str] = Query(default=[], max_length=10),
        categories: list[str] = Query(default=[], max_length=40),
        radius: Literal["500", "1000"] = "500",
        bbox: list[float] | None = Query(default=None, min_length=4, max_length=4),
    ):
        if bbox is not None:
            # FastAPI handles the size; the shared model handles geographic bounds.
            from .domain import DomainError

            try:
                GeometryScope(bbox=tuple(bbox))
            except ValueError:
                raise DomainError("INVALID_BBOX", "Некорректные границы карты") from None
        return repo.poi(snapshot_id, date, route_ids, categories, int(radius), bbox)

    return api
