"""Public response contracts for the offline passenger API."""

from typing import Literal

from pydantic import JsonValue

from .domain import StrictModel, TimeRange

ResearchRow = dict[str, str | int | float | bool | None]


class PassengerMeta(StrictModel):
    dataset_id: str
    coverage: TimeRange
    timezone: Literal["Europe/Moscow"]
    filters: dict[str, JsonValue]
    status: Literal["ready", "unavailable", "incompatible"]
    reason: str | None


class PassengerUnavailable(StrictModel):
    available: Literal[False]
    status: Literal["missing", "corrupt"]
    reason: str


class PassengerMetadata(StrictModel):
    available: Literal[True]
    status: Literal["ready"]
    id: str
    coverage: TimeRange
    timezone: Literal["Europe/Moscow"]
    taxonomy_version: str
    categories: dict[str, str]
    routes: list[str]
    snapshots: list[str]
    total_boardings: int
    unique_cards: int
    source_checks: int
    notes: list[str]
    attribution: str
    sources: list[dict[str, JsonValue]]
    experiments: list[ResearchRow]
    experiment_metrics: list[ResearchRow]
    experiment_intervals: list[ResearchRow]
    fares: list[ResearchRow]


class PassengerCategorySummary(StrictModel):
    category: str
    boardings: int
    share: float | None
    unique_cards: int | None


class PassengerCategoryRow(PassengerCategorySummary):
    route: str
    timestamp: str
    all_boardings: int


class PassengerResult(StrictModel):
    meta: PassengerMeta
    rows: list[PassengerCategoryRow]
    summary: list[PassengerCategorySummary]
    total: int | None


class PassengerSeasonality(StrictModel):
    meta: PassengerMeta
    monthly: list[ResearchRow]
    hourly: list[ResearchRow]
    weekdays: list[ResearchRow]
    comparisons: list[ResearchRow]


class PassengerCohorts(StrictModel):
    meta: PassengerMeta
    monthly: list[ResearchRow]
    autumn: list[ResearchRow]
    autumn_categories: list[ResearchRow]
    category_changes: list[ResearchRow]


class InfrastructureCount(StrictModel):
    kind: str
    value: float | None


class PassengerInfrastructure(StrictModel):
    meta: PassengerMeta
    rows: list[InfrastructureCount]
    source: ResearchRow | None = None
    attribution: str | None = None


class PoiPoint(StrictModel):
    type: Literal["Point"]
    coordinates: tuple[float, float]


class PassengerPoiFeature(StrictModel):
    type: Literal["Feature"]
    id: str
    geometry: PoiPoint
    properties: dict[str, JsonValue]


class PassengerPoiCollection(StrictModel):
    meta: PassengerMeta
    type: Literal["FeatureCollection"]
    features: list[PassengerPoiFeature]
    attribution: str | None = None
