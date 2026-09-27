import csv
import io
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics import NetworkCatalogService, RouteAnalyticsService
from .domain import FINAL_END, HISTORY_END, ROUTES, DomainError, ExportRequest
from .storage import SnapshotStore, atomic_write, file_hash


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def user_csv(store: SnapshotStore, request: ExportRequest):
    # Validate geometry even though it does not change the numeric selection.
    NetworkCatalogService(store).geometry(request.scope)
    view = RouteAnalyticsService(store).view(request.scope)
    frames = view["frames"]
    if request.index is not None:
        if request.index >= len(frames):
            raise DomainError("INVALID_FRAME", "Временной интервал отсутствует")
        frames = [frames[request.index]]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "route",
            "metric",
            "metric_scope",
            "value",
            "unit",
            "interval_start",
            "interval_end",
            "aggregation",
            "provenance",
            "coverage_status",
            "value_origins",
            "quality_flags",
            "snapshot_id",
            "forecast_id",
            "model_id",
            "network_snapshot_id",
            "geometry_filter_affects_metric",
            "estimated_vehicles",
            "vehicle_hours",
            "validations_per_vehicle_hour",
            "fleet_source",
            "fleet_id",
            "fleet_method",
        ]
    )
    meta = view["meta"]
    for frame in frames:
        for value in frame["values"]:
            row = [
                value["routeId"],
                meta["metric"],
                "route",
                value["value"],
                meta["unit"],
                frame["start"],
                frame["end"],
                "sum",
                value["provenance"],
                value["coverageStatus"],
                ",".join(value["valueOrigins"]),
                ",".join(value["qualityFlags"]),
                meta["snapshotId"],
                meta["forecastId"] if value["provenance"] in ("forecast", "mixed") else None,
                meta["modelId"] if value["provenance"] in ("forecast", "mixed") else None,
                meta["networkSnapshotId"],
                "false",
                value["fleetVehicles"],
                value["vehicleHours"],
                value["loadPerVehicleHour"],
                value["fleetSource"],
                meta["fleetId"],
                meta.get("fleetMethod"),
            ]
            writer.writerow([csv_cell(v) for v in row])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def validate_submission_run(store, snapshot_id):
    snapshot = store.read("snapshots", snapshot_id)
    if not snapshot.get("forecastId"):
        raise DomainError("FORECAST_NOT_READY", "Нет выпуска для конкурсного файла", 409)
    run = store.read("forecasts", snapshot["forecastId"])
    if run.get("forecastRunId"):
        canonical = store.read("forecast_runs", run["forecastRunId"])
        model = store.read("trained_models", canonical["spec"]["model_id"])
        run = {**run, "trainingEnd": model["spec"]["time_range"]["end"], "sha256": canonical["sha256"]}
    if (
        datetime.fromisoformat(run["start"]) != HISTORY_END
        or datetime.fromisoformat(run["end"]) != FINAL_END
        or datetime.fromisoformat(run["trainingEnd"]) > HISTORY_END
        or run["rows"] != 14640
    ):
        raise DomainError(
            "INVALID_COMPETITION_RUN", "Нужен полный выпуск ноября–декабря из состояния на 1 ноября"
        )
    return run


class SubmissionService:
    def __init__(self, store: SnapshotStore, dataset: Path):
        self.store, self.dataset = store, dataset

    def build(self, snapshot_id):
        run = validate_submission_run(self.store, snapshot_id)
        path = self.store.path(
            "forecast_runs" if run.get("forecastRunId") else "forecasts", run["id"], "parquet"
        )
        if file_hash(path) != run["sha256"]:
            raise ValueError("Forecast checksum mismatch")
        frame = pd.read_parquet(path)
        frame["route"] = pd.to_numeric(frame.route, errors="raise").astype("int64")
        template = pd.read_csv(
            self.dataset / "test_submission.csv",
            sep=";",
            usecols=["route", "date", "hour"],
            dtype={"route": "int64", "date": str, "hour": "int64"},
        )
        expected = pd.MultiIndex.from_product(
            [ROUTES, pd.date_range("2025-11-01", "2025-12-31").strftime("%Y-%m-%d"), range(24)],
            names=["route", "date", "hour"],
        )
        keys = pd.MultiIndex.from_frame(template[["route", "date", "hour"]])
        if len(keys) != 14640 or keys.has_duplicates or set(keys) != set(expected):
            raise ValueError("Invalid submission template keys")
        frame["date"], frame["hour"] = frame.timestamp.dt.strftime("%Y-%m-%d"), frame.timestamp.dt.hour
        forecast_keys = pd.MultiIndex.from_frame(frame[["route", "date", "hour"]])
        if len(forecast_keys) != 14640 or forecast_keys.has_duplicates or set(forecast_keys) != set(expected):
            raise ValueError("Invalid forecast keys")
        values = frame.set_index(["route", "date", "hour"]).value.reindex(keys).to_numpy()
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("Invalid submission values")
        template["prediction"] = np.floor(values + 0.5).astype("int64")
        return template.to_csv(index=False, sep=";", lineterminator="\n").encode("utf-8")

    def write(self, snapshot_id, destination):
        atomic_write(destination, self.build(snapshot_id))
        return {
            "path": str(destination),
            "rows": 14640,
            "sha256": file_hash(destination),
            "snapshotId": snapshot_id,
        }
