import io
import json
import zipfile
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from .domain import DomainError, StrictModel, TimeRange
from .schedules import ScheduleService
from .storage import atomic_write, canonical, file_hash


class PeriodExportSpec(StrictModel):
    dataset_id: str
    forecast_id: str | None = None
    route_ids: list[str] = Field(min_length=1, max_length=1000)
    time_range: TimeRange
    mode: Literal["auto", "history", "forecast"] = "auto"
    grain: Literal["hour", "day", "month"] = "hour"
    format: Literal["csv", "parquet", "competition"] = "csv"
    scenario_id: str | None = None
    metric_scope: Literal["route", "stop", "segment"] = "route"
    object_ids: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    schedule_id: str | None = None
    schedule_scenario: bool = False


class PeriodExporter:
    def __init__(self, settings, data, models):
        self.settings, self.data, self.models = settings, data, models

    def frame(self, spec: PeriodExportSpec):
        self.data.manifest(spec.dataset_id)
        if spec.grain != "hour" and (spec.time_range.start.hour or spec.time_range.end.hour):
            raise DomainError("INVALID_DAY_RANGE", "Для суток и месяцев нужны границы в 00:00")
        if spec.grain == "month" and (spec.time_range.start.day != 1 or spec.time_range.end.day != 1):
            raise DomainError("INVALID_MONTH_RANGE", "Для месячной агрегации нужны первые числа месяцев")
        hours = int((spec.time_range.end - spec.time_range.start).total_seconds() / 3600)
        if hours * len(spec.route_ids) > 5_000_000 or len(set(spec.route_ids)) != len(spec.route_ids):
            raise DomainError("INVALID_EXPORT_RANGE", "Повтор маршрутов или слишком большой запрос")
        times = pd.date_range(spec.time_range.start, spec.time_range.end, freq="h", inclusive="left")
        frame = pd.MultiIndex.from_product([spec.route_ids, times], names=["route", "timestamp"]).to_frame(
            index=False
        )
        frame["value"], frame["provenance"] = np.nan, "missing"
        forecast = None
        if spec.mode in ("auto", "forecast") and spec.forecast_id:
            forecast, predicted = self.models.frame(
                spec.forecast_id, spec.time_range.start, spec.time_range.end, spec.route_ids
            )
            if forecast["spec"]["dataset_id"] != spec.dataset_id:
                raise DomainError(
                    "VERSION_MISMATCH", "Прогноз и факты должны ссылаться на одну версию данных", 409
                )
            frame = frame.merge(
                predicted.rename(columns={"value": "prediction"}),
                on=["route", "timestamp"],
                how="left",
                validate="one_to_one",
            )
            frame["value"] = frame.pop("prediction")
            frame.loc[frame.value.notna(), "provenance"] = "forecast"
        if spec.mode in ("auto", "history"):
            history = self.data.frame(
                spec.dataset_id,
                route_ids=spec.route_ids,
                start=spec.time_range.start,
                end=spec.time_range.end,
            )
            frame = frame.merge(
                history[["route", "timestamp", "value"]].rename(columns={"value": "observation"}),
                on=["route", "timestamp"],
                how="left",
                validate="one_to_one",
            )
            known = frame.observation.notna()
            frame.loc[known, "value"] = frame.loc[known, "observation"]
            frame.loc[known, "provenance"] = "observation"
            frame.drop(columns="observation", inplace=True)
        frame["vehicle_hours"], frame["fleet_method"] = np.nan, "missing"
        frame["schedule_scenario"] = spec.schedule_scenario
        if spec.schedule_id:
            fleet = pd.DataFrame(
                ScheduleService(self.settings.data_root).hours(
                    spec.schedule_id, spec.time_range, spec.route_ids, scenario=spec.schedule_scenario
                )
            )
            fleet.timestamp = pd.to_datetime(fleet.timestamp, utc=True).dt.tz_convert("Europe/Moscow")
            frame = frame.drop(columns=["vehicle_hours", "fleet_method"]).merge(
                fleet[["route", "timestamp", "vehicle_hours", "method"]].rename(
                    columns={"method": "fleet_method"}
                ),
                on=["route", "timestamp"],
                how="left",
                validate="one_to_one",
            )
        if not spec.schedule_id:
            from .fleet import FleetService
            from .storage import digest

            history_id = "history-" + digest({"dataset": spec.dataset_id, "adapter": 1})
            candidates = [
                json.loads(path.read_bytes()) for path in (self.models.store.root / "fleets").glob("*.json")
            ]
            matching = next((fleet for fleet in candidates if fleet.get("historyId") == history_id), None)
            if matching:
                service = FleetService(self.models.store, self.settings.data_root)
                for route, positions in frame.groupby("route", sort=False).indices.items():
                    rows = frame.iloc[positions]
                    sources = rows.provenance.map({"observation": 1, "forecast": 2, "missing": 0}).tolist()
                    records = service.hours(
                        {"historyId": history_id, "fleetId": matching["id"]},
                        spec.time_range.start,
                        route,
                        sources,
                        forecast["spec"]["origin"] if forecast else None,
                    )
                    frame.loc[positions, "vehicle_hours"] = [record["vehicles"] for record in records]
                    frame.loc[positions, "fleet_method"] = [record["source"] for record in records]
        frame["base_value"] = frame.value
        frame["base_vehicle_hours"] = frame.vehicle_hours
        frame["additional_vehicle_hours"] = 0.0
        if spec.scenario_id:
            from .scenarios import ScenarioService

            frame = ScenarioService(self.models.store).apply(frame, spec.scenario_id, spec.forecast_id)
        if spec.metric_scope != "route":
            from .spatial import SpatialService

            if not spec.snapshot_id:
                raise DomainError("GEOMETRY_REQUIRED", "Для детализации нужен snapshot_id геометрии")
            frame.attrs["lineage"] = {
                "dataset_id": spec.dataset_id,
                "forecast_id": spec.forecast_id,
                "model_id": forecast["spec"]["model_id"] if forecast else None,
                "origin": forecast["spec"]["origin"] if forecast else None,
                "schedule_id": spec.schedule_id,
                "scenario_id": spec.scenario_id,
                "grain": spec.grain,
            }
            return SpatialService(self.models.store, self.settings.data_root).frame(frame, spec)
        if spec.grain != "hour":
            frame.timestamp = aggregate_timestamp(frame.timestamp, spec.grain)

            def complete(values):
                return values.sum() if values.notna().all() else np.nan

            def source(values):
                return values.iloc[0] if values.nunique() == 1 else "mixed"

            frame = frame.groupby(["route", "timestamp"], as_index=False).agg(
                value=("value", complete),
                base_value=("base_value", complete),
                vehicle_hours=("vehicle_hours", complete),
                base_vehicle_hours=("base_vehicle_hours", complete),
                additional_vehicle_hours=("additional_vehicle_hours", "sum"),
                provenance=("provenance", source),
                fleet_method=("fleet_method", source),
                schedule_scenario=("schedule_scenario", "any"),
            )
        frame["validations_per_vehicle_hour"] = frame.value / frame.vehicle_hours.where(
            frame.vehicle_hours > 0
        )
        for column, value in {
            "dataset_id": spec.dataset_id,
            "forecast_id": spec.forecast_id,
            "model_id": forecast["spec"]["model_id"] if forecast else None,
            "origin": forecast["spec"]["origin"] if forecast else None,
            "schedule_id": spec.schedule_id,
            "scenario_id": spec.scenario_id,
            "metric": "successful_validations",
            "grain": spec.grain,
            "availability_policy": "retrospective_event_time",
        }.items():
            frame[column] = value
        frame["coefficient"] = frame.value / frame.base_value.where(frame.base_value > 0)
        return frame

    def write(self, spec, ident):
        frame = self.frame(spec)
        metadata = {
            "spec": spec.model_dump(mode="json"),
            "rows": len(frame),
            "timezone": "Europe/Moscow",
            "float_policy": "unrounded",
        }
        extension = "csv"
        if spec.format == "competition":
            frame = self._competition(spec, frame)
        if spec.format == "parquet":
            extension = "zip"
            buffer, parquet = io.BytesIO(), io.BytesIO()
            frame.to_parquet(parquet, index=False)
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
                output.writestr("route_hours.parquet", parquet.getvalue())
                output.writestr("manifest.json", canonical(metadata))
            content = buffer.getvalue()
        else:
            content = frame.to_csv(index=False, sep=";", lineterminator="\n").encode("utf-8-sig")
        path = self.models.store.path("exports", ident, extension)
        atomic_write(path, content)
        return {
            **metadata,
            "filename": path.name,
            "sha256": file_hash(path),
            "download_url": f"/api/v2/jobs/{ident}/download",
        }

    def _competition(self, spec, frame):
        if (
            not spec.forecast_id
            or spec.mode != "forecast"
            or spec.grain != "hour"
            or spec.scenario_id
            or spec.metric_scope != "route"
        ):
            raise DomainError("INVALID_COMPETITION_EXPORT", "Конкурсный экспорт требует почасовой прогноз")
        run = self.models.store.read("forecast_runs", spec.forecast_id)
        if pd.Timestamp(run["spec"]["origin"]) != pd.Timestamp("2025-11-01T00:00:00+03:00"):
            raise DomainError("COMPETITION_CUTOFF", "Конкурсный origin должен быть 01.11.2025 00:00 МСК")
        path = self.settings.input_path("test_submission.csv")
        if file_hash(path) != "71c8126bb51068c936462a170da885bad860fc91a58b3d2ac731876d324d7ec3":
            raise DomainError("TEMPLATE_CHANGED", "Контрольная сумма конкурсного шаблона изменилась")
        template = pd.read_csv(path, sep=";", dtype={"route": str})[["route", "date", "hour"]]
        values = frame.assign(date=frame.timestamp.dt.strftime("%Y-%m-%d"), hour=frame.timestamp.dt.hour)
        result = template.merge(
            values[["route", "date", "hour", "value"]],
            on=["route", "date", "hour"],
            how="left",
            validate="one_to_one",
        )
        if (
            len(result) != 14640
            or result.value.isna().any()
            or not np.isfinite(result.value).all()
            or (result.value < 0).any()
        ):
            raise DomainError("INCOMPLETE_COMPETITION_EXPORT", "Прогноз не покрывает все ключи шаблона")
        result["prediction"] = np.floor(result.pop("value") + 0.5).astype(np.int64)
        return result


def aggregate_timestamp(timestamps, grain):
    if grain == "month":
        return pd.to_datetime(timestamps.dt.strftime("%Y-%m-01")).dt.tz_localize("Europe/Moscow")
    return timestamps.dt.floor("D")
