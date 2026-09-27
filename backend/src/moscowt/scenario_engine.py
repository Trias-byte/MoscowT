"""Explicit scenario inference; immutable results become readable only after queue completion."""

import io
from datetime import datetime

import numpy as np
import pandas as pd

from .domain import TZ, DomainError
from .modeling.contracts import ForecastSpec
from .platform_exports import PeriodExporter, PeriodExportSpec
from .scenarios import ScenarioSpec
from .schedules import ScheduleService, schedule_table
from .storage import atomic_write, digest, file_hash


def availability(start, end, restrictions):
    """Integrate the strongest simultaneous restriction, never multiply overlapping closures."""
    intervals = [
        (max(start, a), min(end, b), reduction) for a, b, reduction in restrictions if a < end and b > start
    ]
    cuts = sorted({start, end, *(a for a, _, _ in intervals), *(b for _, b, _ in intervals)})
    unavailable = sum(
        (b - a).total_seconds() * max((r for x, y, r in intervals if x < b and y > a), default=0)
        for a, b in zip(cuts, cuts[1:])
    )
    return 1 - unavailable / (end - start).total_seconds()


def service_fraction(stamp, start_minute, end_minute):
    if start_minute is None or start_minute == end_minute:
        return 1.0
    end_minute += 1440 if end_minute < start_minute else 0
    duration = 0.0
    for day in (stamp.normalize() - pd.Timedelta(days=1), stamp.normalize()):
        start, end = day + pd.Timedelta(minutes=start_minute), day + pd.Timedelta(minutes=end_minute)
        duration += max(0, (min(end, stamp + pd.Timedelta(hours=1)) - max(start, stamp)).total_seconds())
    return duration / 3600


def demand_ratio(ratio, elasticity):
    ratio = np.asarray(ratio, dtype=float)
    return np.where(ratio <= 0, 0.0, ratio**elasticity)


def timetable_departures(service, ident, time_range, routes, reuse):
    manifest = service.store.read("schedules", ident)
    table = schedule_table(service.store.root, ident)
    counts = {}
    for day in pd.date_range(
        pd.Timestamp(time_range.start).normalize() - pd.Timedelta(days=1), time_range.end, freq="D"
    ):
        applicable = manifest["valid_from"] <= str(day.date()) < manifest["valid_to"]
        if manifest["method"] == "stop_timetable_estimate":
            selected = table.loc[table.weekday.eq(day.dayofweek)] if applicable or reuse else table.iloc[:0]
        else:
            selected = table.loc[table.service_date.eq(str(day.date()))] if applicable else table.iloc[:0]
        for row in selected.loc[selected.route.isin(routes)].itertuples():
            stamp = (day + pd.Timedelta(minutes=row.start_minute)).floor("h")
            counts[row.route, stamp] = counts.get((row.route, stamp), 0) + 1
    fleet = pd.DataFrame(service.hours(ident, time_range, routes, scenario=reuse))
    fleet.timestamp = pd.to_datetime(fleet.timestamp, utc=True).dt.tz_convert("Europe/Moscow")
    fleet["departures"] = [
        counts.get((r.route, r.timestamp), 0) if pd.notna(r.vehicle_hours) else np.nan
        for r in fleet.itertuples()
    ]
    return fleet


class ScenarioForecastService:
    VERSION = "weather-inference-service-elasticity-v2"

    def __init__(self, settings, data, models):
        self.settings, self.data, self.models, self.store = settings, data, models, models.store

    def run(self, scenario_id, job_id):
        ident = "scenario-" + digest({"draft": scenario_id, "job": job_id, "version": self.VERSION})
        if self.store.path("scenarios", ident).exists():
            existing = self.store.read("scenarios", ident)
            if file_hash(self.store.path("scenario_results", ident, "parquet")) != existing["sha256"]:
                raise DomainError("SCENARIO_CORRUPTED", "Контрольная сумма сценария не совпадает")
            return existing
        source = self.store.read("scenarios", scenario_id)
        spec = ScenarioSpec.model_validate(source["spec"])
        if spec.engine != "recompute":
            raise DomainError("SCENARIO_ENGINE_REQUIRED", "Для пересчёта создайте сценарий нового формата")
        run = self.store.read("forecast_runs", spec.forecast_id)
        if run["spec"].get("diagnostic_observed_factors"):
            raise DomainError("DIAGNOSTIC_ONLY", "Для сценария выберите оперативный базовый выпуск")
        selected = ForecastSpec.model_validate(
            {**run["spec"], "route_ids": spec.route_ids, "time_range": spec.time_range.model_dump()}
        )
        export = PeriodExportSpec(
            dataset_id=selected.dataset_id,
            forecast_id=spec.forecast_id,
            route_ids=spec.route_ids,
            time_range=spec.time_range,
            mode="forecast",
            schedule_id=spec.schedule.base_schedule_id,
            schedule_scenario=spec.schedule.allow_period_reuse,
        )
        frame = PeriodExporter(self.settings, self.data, self.models).frame(export)
        if frame.value.isna().any():
            raise DomainError("SCENARIO_BASE_MISSING", "Базовый выпуск не покрывает сценарий")
        frame["base_value"], frame["base_vehicle_hours"] = frame.value.copy(), frame.vehicle_hours.copy()
        warnings, curves, weather_curves = [], {}, {}
        changes = spec.weather.model_dump(exclude_none=True)
        manifest = self.store.read("trained_models", selected.model_id)
        if changes:
            override = {
                "route_ids": spec.route_ids,
                "start": spec.time_range.start,
                "end": spec.time_range.end,
                "weather": changes,
            }
            predicted = self.models.predict(selected, overrides=override, persist=False)
            frame["value"] = (
                frame[["route", "timestamp"]]
                .merge(predicted, on=["route", "timestamp"], validate="one_to_one")
                .value.to_numpy()
            )
            for field, value in changes.items():
                limits = manifest.get("feature_ranges", {}).get("weather_" + field)
                if limits and not limits["min"] <= value <= limits["max"]:
                    warnings.append(
                        f"{field}: значение вне диапазона обучения {limits['min']}…{limits['max']}"
                    )
                values = {
                    "temperature_2m": [max(-60, value - 5), value, min(60, value + 5)],
                    "relative_humidity_2m": [max(0, value - 20), value, min(100, value + 20)],
                    "precipitation": [0, value, min(200, value + 5)],
                }[field]
                weather_curves[field] = []
                for x in sorted(set(values)):
                    variant = {**override, "weather": {**changes, field: x}}
                    prediction = self.models.predict(selected, overrides=variant, persist=False)
                    values = (
                        frame[["route", "timestamp"]]
                        .merge(prediction, on=["route", "timestamp"], validate="one_to_one")
                        .value.to_numpy()
                    )
                    weather_curves[field].append((x, values))
        schedule = spec.schedule
        service = ScheduleService(self.settings.data_root)
        base_frequency = np.full(len(frame), np.nan)
        ratio = np.ones(len(frame))
        fleet = frame.vehicle_hours.to_numpy(dtype=float, copy=True)
        has_schedule_change = any(
            [
                schedule.schedule_id,
                schedule.headway_minutes is not None,
                schedule.service_start_minute is not None,
                schedule.departures,
            ]
        )
        if schedule.base_schedule_id:
            base = timetable_departures(
                service,
                schedule.base_schedule_id,
                spec.time_range,
                spec.route_ids,
                schedule.allow_period_reuse,
            )
            base_frequency = (
                frame[["route", "timestamp"]]
                .merge(base, on=["route", "timestamp"], validate="one_to_one")
                .departures.to_numpy()
            )
        elif schedule.base_headway_minutes:
            base_frequency[:] = 60 / schedule.base_headway_minutes
            warnings.append("Исходный интервал движения задан пользователем; это допущение")
        new_frequency = base_frequency.copy()
        if has_schedule_change:
            if not np.isfinite(base_frequency).all():
                raise DomainError(
                    "BASE_SCHEDULE_REQUIRED",
                    "Для изменения расписания нужен полный исходный график или явно заданный исходный интервал",
                )
            if schedule.schedule_id:
                alternative = timetable_departures(
                    service,
                    schedule.schedule_id,
                    spec.time_range,
                    spec.route_ids,
                    schedule.allow_period_reuse,
                )
                alternative = frame[["route", "timestamp"]].merge(
                    alternative, on=["route", "timestamp"], validate="one_to_one"
                )
                new_frequency, fleet = (
                    alternative.departures.to_numpy(),
                    alternative.vehicle_hours.to_numpy(dtype=float),
                )
                if not np.isfinite(new_frequency).all():
                    raise DomainError(
                        "SCHEDULE_COVERAGE_MISSING",
                        "Альтернативное расписание неполно или не действует в выбранный период",
                    )
            elif schedule.headway_minutes:
                new_frequency[:] = 60 / schedule.headway_minutes
            fractions = np.array(
                [
                    service_fraction(t, schedule.service_start_minute, schedule.service_end_minute)
                    for t in frame.timestamp
                ]
            )
            new_frequency *= fractions
            departure_hours = np.zeros(len(frame))
            for edit in schedule.departures:
                at = pd.Timestamp(edit.timestamp)
                mask = frame.route.eq(edit.route_id) & frame.timestamp.eq(at.floor("h"))
                new_frequency[mask] += edit.change
                for position in np.flatnonzero(frame.route.eq(edit.route_id)):
                    stamp = frame.timestamp.iloc[position]
                    overlap = max(
                        0,
                        (
                            min(
                                at + pd.Timedelta(minutes=edit.duration_minutes),
                                stamp + pd.Timedelta(hours=1),
                            )
                            - max(at, stamp)
                        ).total_seconds(),
                    )
                    departure_hours[position] += edit.change * overlap / 3600
            if (new_frequency < 0).any():
                raise DomainError(
                    "NEGATIVE_DEPARTURES", "Нельзя удалить больше отправлений, чем есть в расписании"
                )
            if ((base_frequency == 0) & (new_frequency > 0)).any():
                raise DomainError(
                    "NO_BASE_SERVICE",
                    "Нет базового спроса для новых часов обслуживания; нужна отдельная оценка",
                )
            ratio = np.divide(
                new_frequency, base_frequency, out=np.ones(len(frame)), where=base_frequency > 0
            )
            if not schedule.schedule_id:
                # Estimated resource hours follow frequency; explicit trip edits use their stated durations.
                before_edits = new_frequency.copy()
                for edit in schedule.departures:
                    before_edits[
                        frame.route.eq(edit.route_id)
                        & frame.timestamp.eq(pd.Timestamp(edit.timestamp).floor("h"))
                    ] -= edit.change
                resource_ratio = np.divide(
                    before_edits, base_frequency, out=np.ones(len(frame)), where=base_frequency > 0
                )
                fleet = fleet * resource_ratio + departure_hours
            else:
                fleet = fleet * fractions + departure_hours
            fleet = np.maximum(0, fleet)
        accident_availability = np.ones(len(frame))
        if spec.incidents:
            for route, positions in frame.groupby("route").indices.items():
                restrictions = [
                    (
                        pd.Timestamp(i.start),
                        pd.Timestamp(i.start) + pd.Timedelta(minutes=i.duration_minutes),
                        i.reduction,
                    )
                    for i in spec.incidents
                    if route in i.route_ids
                ]
                for position in positions:
                    stamp = frame.timestamp.iloc[position]
                    accident_availability[position] = availability(
                        stamp, stamp + pd.Timedelta(hours=1), restrictions
                    )
            warnings.append(
                "ДТП: длительность и потеря движения заданы пользователем; близость к линии не доказывает блокировку"
            )
        ratio *= accident_availability
        fleet *= accident_availability
        if has_schedule_change or spec.incidents:
            warnings.append(
                f"Реакция спроса: ε={schedule.elasticity}; демонстрационное допущение, не измеренная причинная связь"
            )
        weather_prediction = frame.value.to_numpy(copy=True)
        supply_factor = demand_ratio(ratio, schedule.elasticity)
        frame["value"] *= supply_factor * spec.coefficients.multiplier
        # A complete interruption remains zero even for elasticity=0.
        frame.loc[ratio <= 0, "value"] = 0.0
        extra = spec.additional_vehicle_hours / len(frame)
        frame["vehicle_hours"] = fleet + extra
        if extra:
            warnings.append(
                "Дополнительные вагоно-часы распределены равномерно; неизвестный исходный выпуск остаётся неизвестным"
            )
        frame["additional_vehicle_hours"] = frame.vehicle_hours - frame.base_vehicle_hours
        if has_schedule_change or spec.incidents or extra:
            frame["fleet_method"] = "scenario_service_assumption"
        frame["effective_service_ratio"] = ratio
        frame["validations_per_vehicle_hour"] = frame.value / frame.vehicle_hours.where(
            frame.vehicle_hours > 0
        )
        for field, points in weather_curves.items():
            curves[field] = [
                {"x": x, "value": float((values * supply_factor * spec.coefficients.multiplier).sum())}
                for x, values in points
            ]
        curves["service_ratio"] = [
            {
                "x": x,
                "value": float(
                    (
                        weather_prediction
                        * demand_ratio(x * accident_availability, schedule.elasticity)
                        * spec.coefficients.multiplier
                    ).sum()
                ),
            }
            for x in (0, 0.5, 1, 1.5, 2)
        ]
        curves["season"] = [
            {
                "x": x,
                "value": float(
                    (
                        weather_prediction
                        * supply_factor
                        * spec.coefficients.weather
                        * spec.coefficients.event
                        * x
                    ).sum()
                ),
            }
            for x in (0.5, 0.75, 1, 1.25, 1.5)
        ]
        output = io.BytesIO()
        frame.to_parquet(output, index=False)
        path = self.store.path("scenario_results", ident, "parquet")
        atomic_write(path, output.getvalue())

        def total(column):
            return float(frame[column].sum()) if frame[column].notna().all() else None

        result = {
            "id": ident,
            "spec": spec.model_dump(mode="json"),
            "method": self.VERSION,
            "draft_id": scenario_id,
            "job_id": job_id,
            "result_file": ident,
            "sha256": file_hash(path),
            "created_at": datetime.now(TZ).isoformat(),
            "model_id": selected.model_id,
            "dataset_id": selected.dataset_id,
            "origin": selected.origin.isoformat(),
            "rows": len(frame),
            "base_total": total("base_value"),
            "total": total("value"),
            "base_vehicle_hours": total("base_vehicle_hours"),
            "vehicle_hours": total("vehicle_hours"),
            "warnings": warnings,
            "sensitivity": curves,
            "feature_ranges": manifest.get("feature_ranges", {}),
            "sensitivity_note": "Остальные параметры зафиксированы на значениях сценария. Кривая расписания заменяет отношение частот одинаково во всех выбранных часах, сохраняя ДТП. Расписание/сезон — заданные допущения.",
            "metric": "successful_validations",
        }
        self.store.put("scenarios", ident, result)
        return result
