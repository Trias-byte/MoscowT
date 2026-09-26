"""Public response schemas. Serialization is shared with the frontend Zod contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class Range(WireModel):
    start: str
    end: str


class Meta(WireModel):
    contractVersion: Literal[2]
    snapshotId: str
    networkSnapshotId: str | None
    historyId: str
    fleetId: str | None = None
    fleetMethod: str | None = None
    metric: Literal["successful_validations"]
    metricScope: Literal["route"]
    unit: Literal["validations"]
    intervalUnit: Literal["validations/hour", "validations/day"]
    aggregation: Literal["sum"]
    grain: Literal["hour", "day"]
    timeRange: Range
    provenance: Literal["observation", "forecast", "mixed", "missing"]
    forecastId: str | None
    modelId: str | None
    issuedAt: str | None
    createdAt: str | None
    geometryFilterAffectsMetric: Literal[False]
    baselineDescription: str
    coverageNote: str


class RouteValue(WireModel):
    routeId: str
    provenance: Literal["observation", "forecast", "mixed", "missing"]
    value: float | None = Field(ge=0)
    baseline: float | None = Field(ge=0)
    baselineCount: float | None
    valueOrigins: list[str]
    qualityFlags: list[str]
    coverageStatus: str
    historySupport: str
    fleetVehicles: float | None = Field(default=None, ge=0)
    vehicleHours: float | None = Field(default=None, ge=0)
    loadPerVehicleHour: float | None = Field(default=None, ge=0)
    fleetSource: Literal["observed", "estimated", "mixed", "missing"] = "missing"
    fleetSampleDays: int = 0
    fleetCoverage: float | None = Field(default=None, ge=0, le=1)


class Frame(WireModel):
    start: str
    end: str
    values: list[RouteValue]
    aggregate: float | None
    baseline: float | None


class GeometrySelection(WireModel):
    networkSnapshotId: str
    patternIds: list[str]
    stopIds: list[str]
    segmentIds: list[str]
    geometryQuality: Literal["schematic", "mapped"]
    asOf: str
    referenceMode: Literal["reference", "historical"]
    historicallyUnavailablePatternIds: list[str]
    missingGeometryRouteIds: list[str]
    geometryFilterAffectsMetric: Literal[False]
    warning: str


class SnapshotResponse(WireModel):
    meta: Meta
    frames: list[Frame]
    geometry: GeometrySelection


class Series(WireModel):
    routeId: str
    points: list[float | None]
    baseline: list[float | None]


class SeriesResponse(WireModel):
    meta: Meta
    series: list[Series]


class Comparison(WireModel):
    routeId: str
    value: float | None
    baseline: float | None
    difference: float | None


class ComparisonResponse(WireModel):
    meta: Meta
    routes: list[Comparison]


class HeatmapResponse(WireModel):
    meta: Meta
    routeIds: list[str]
    cells: list[tuple[int, int, float | None]]


class Route(WireModel):
    id: str
    number: str
    name: str
    isTarget: bool
    hasData: bool
    hasGeometry: bool
    historySupport: str


class Pattern(WireModel):
    id: str
    routeId: str
    direction: int
    name: str
    validFrom: str
    validTo: str | None
    observedAt: str


class Stop(WireModel):
    id: str
    stationId: str
    name: str
    routeId: str
    patternId: str
    direction: int
    order: int
    coordinates: tuple[float, float]


class Segment(WireModel):
    id: str
    routeId: str
    patternId: str
    fromId: str
    toId: str
    order: int
    coordinates: list[tuple[float, float]]


class NetworkResponse(WireModel):
    networkSnapshotId: str
    geometryQuality: Literal["schematic", "mapped"]
    asOf: str
    warning: str
    routes: list[Route]
    patterns: list[Pattern]
    stops: list[Stop]
    segments: list[Segment]
    missingGeometryRouteIds: list[str]


class CapabilitiesResponse(WireModel):
    contractVersion: Literal[2]
    snapshotId: str
    networkSnapshotId: str | None
    metrics: list[Literal["successful_validations"]]
    modes: list[Literal["auto", "history", "forecast"]]
    horizons: list[Literal["day", "month", "competition_61d"]]
    stream: Literal[False]
    historyRange: Range | None
    forecastRange: Range | None
    forecastId: str | None
    defaultDate: str
    targetRouteIds: list[str]
    absoluteThresholds: list[float]
    fleetId: str | None = None
    vehicleLoadThresholds: list[float]
    submissionAvailable: bool


class JobResponse(WireModel):
    id: str
    status: Literal["pending", "running", "ready", "failed"]
    kind: str
    downloadUrl: str | None
    error: str | None
