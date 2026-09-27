"""Versioned schedules and interval integration; plans never imply observed service."""

import calendar
import io
import json
import re
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd

from .constants.schedules import (
    MONTHS as MONTHS,
    WEEKDAYS as WEEKDAYS,
)
from .domain import TZ, DomainError, TimeRange
from .storage import SnapshotStore, atomic_write, digest, file_hash


def minute(value):
    match = re.fullmatch(r"(\d{1,2}):([0-5]\d)", str(value))
    if not match or int(match[1]) > 47:
        return None
    return int(match[1]) * 60 + int(match[2])


def normal_id(value):
    """Preserve original identifiers elsewhere; compare decimal identifiers without padding."""
    text = str(value).strip()
    return str(int(text)) if text.isdecimal() else text


def union_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def integrate(intervals, time_range: TimeRange):
    """Union within physical resources, flag cross-route collisions instead of double counting."""
    hours, conflicts = {}, set()
    by_resource = {}
    for item in intervals:
        by_resource.setdefault(item["resource"], []).append(item)
    for items in by_resource.values():
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                if left["route"] != right["route"] and max(left["start"], right["start"]) < min(
                    left["end"], right["end"]
                ):
                    conflicts.update((left["route"], right["route"]))
        by_route = {}
        for item in items:
            by_route.setdefault(item["route"], []).append((item["start"], item["end"]))
        for route, spans in by_route.items():
            for start, end in union_intervals(spans):
                start, end = max(start, time_range.start), min(end, time_range.end)
                cursor = start.replace(minute=0, second=0, microsecond=0)
                while cursor < end:
                    overlap = (
                        min(end, cursor + timedelta(hours=1)) - max(start, cursor)
                    ).total_seconds() / 3600
                    key = route, cursor
                    hours[key] = hours.get(key, 0.0) + max(0, overlap)
                    cursor += timedelta(hours=1)
    return hours, conflicts


def schedule_table(root, ident):
    store = SnapshotStore(root)
    manifest = store.read("schedules", ident)
    path = store.path("schedules", ident, "parquet")
    try:
        stat = path.stat()
        return _schedule_table(str(path), stat.st_mtime_ns, stat.st_size, manifest["sha256"])
    except (OSError, ValueError, KeyError) as error:
        raise DomainError(
            "SCHEDULE_CORRUPTED",
            "Файл расписания отсутствует или повреждён. Загрузите расписание заново.",
            503,
        ) from error


@lru_cache(maxsize=16)
def _schedule_table(path, modified, size, checksum):
    from pathlib import Path

    if file_hash(Path(path)) != checksum:
        raise ValueError("Schedule checksum mismatch")
    return pd.read_parquet(path)


class ScheduleService:
    def __init__(self, root):
        self.store = SnapshotStore(root)

    def list(self):
        return [
            json.loads(path.read_bytes()) for path in sorted((self.store.root / "schedules").glob("*.json"))
        ]

    def import_file(self, path):
        sha = file_hash(path)
        ident = "schedule-" + digest({"source": sha, "schema": 1})
        if self.store.path("schedules", ident).exists():
            return self.store.read("schedules", ident)
        frame = pd.read_csv(
            path, sep=None, engine="python", dtype=str, keep_default_na=False, encoding="utf-8-sig"
        )
        if "маршрут" in frame:
            trips, metadata = self._stop_times(frame)
        else:
            trips, metadata = self._duties(frame)
        buffer = io.BytesIO()
        trips.to_parquet(buffer, index=False)
        target = self.store.path("schedules", ident, "parquet")
        atomic_write(target, buffer.getvalue())
        # Source is streamed, not retained as a second in-memory copy.
        import shutil

        source = self.store.path("schedule_sources", ident, "csv")
        source.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as src, source.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        result = {
            "id": ident,
            "schema_version": 1,
            "source_sha256": sha,
            "sha256": file_hash(target),
            "input_rows": len(frame),
            "trips": len(trips),
            "routes": sorted(trips.route.unique()),
            **metadata,
        }
        self.store.put("schedules", ident, result)
        return result

    def _stop_times(self, frame):
        required = {
            "маршрут",
            "направление",
            "порядок_остановки_на_странице",
            "остановка",
            "день_недели",
            "время_прибытия",
            "период_на_сайте",
        }
        if not required <= set(frame):
            raise DomainError("INVALID_SCHEDULE", "Нет обязательных колонок остановочного расписания")
        periods = frame["период_на_сайте"].unique()
        match = re.fullmatch(r"на (\S+) (\d{4}) года", periods[0]) if len(periods) == 1 else None
        if not match or match[1] not in MONTHS:
            raise DomainError(
                "UNKNOWN_SERVICE_CALENDAR", "Не удалось определить единый месяц действия расписания"
            )
        year, month = int(match[2]), MONTHS.index(match[1]) + 1
        start = date(year, month, 1)
        end = start + timedelta(days=calendar.monthrange(year, month)[1])
        rows, incomplete, invalid = [], set(), 0
        for (route, direction, day), group in frame.groupby(
            ["маршрут", "направление", "день_недели"], sort=False
        ):
            if day not in WEEKDAYS:
                raise DomainError("INVALID_SCHEDULE", "Неизвестный день недели")
            stops = [
                (int(order), stop)
                for order, stop in group.groupby("порядок_остановки_на_странице", sort=False)
            ]
            stops.sort(key=lambda item: item[0])
            lengths = {len(stop) for _, stop in stops}
            if len(lengths) != 1 or len(stops) < 2:
                incomplete.add((route, WEEKDAYS[day]))
                invalid += len(group)
                continue
            times = [[minute(v) for v in stop["время_прибытия"]] for _, stop in stops]
            for index in range(len(times[0])):
                arrivals = [stop[index] for stop in times]
                if any(value is None for value in arrivals):
                    invalid += 1
                    incomplete.add((route, WEEKDAYS[day]))
                    continue
                first = arrivals[0]
                # Service day starts at 03:00 for this estimate, explicitly recorded below.
                first += 1440 if first < 180 else 0
                unfolded = [first]
                for value in arrivals[1:]:
                    while value < unfolded[-1]:
                        value += 1440
                    unfolded.append(value)
                if not 0 < unfolded[-1] - first <= 240:
                    invalid += 1
                    incomplete.add((route, WEEKDAYS[day]))
                    continue
                rows.append(
                    {
                        "route": route,
                        "direction": direction,
                        "weekday": WEEKDAYS[day],
                        "start_minute": first,
                        "end_minute": unfolded[-1],
                        "trip": str(index),
                        "duty": "",
                        "garage": "",
                    }
                )
        if not rows:
            raise DomainError("INVALID_SCHEDULE", "Нет пригодных интервалов расписания")
        return pd.DataFrame(rows), {
            "method": "stop_timetable_estimate",
            "valid_from": str(start),
            "valid_to": str(end),
            "invalid_trips": invalid,
            "incomplete_route_weekdays": [list(key) for key in sorted(incomplete)],
            "assumptions": [
                "Рейсы восстановлены по порядку строк на каждой остановке; идентификаторов рейса и выхода нет",
                "Служебные сутки начинаются в 03:00; отстой и резерв не включены",
                "Число одновременных рейсов — оценка занятых ресурсов, а не подтверждённое число вагонов",
            ],
            "assignment_coverage": None,
        }

    def _duties(self, frame):
        required = {"route", "service_date", "grafic", "trip_num", "direction", "start", "end"}
        if not required <= set(frame):
            raise DomainError(
                "INVALID_SCHEDULE", "Для выходов нужны route,service_date,grafic,trip_num,direction,start,end"
            )
        rows = []
        for row in frame.drop_duplicates().to_dict("records"):
            try:
                day = date.fromisoformat(row["service_date"])
                start, end = minute(row["start"]), minute(row["end"])
                if start is None or end is None or not row["route"].strip() or not row["grafic"].strip():
                    raise ValueError()
                if end < start:
                    end += 1440
                if not 0 < end - start <= 24 * 60:
                    raise ValueError()
            except ValueError:
                raise DomainError("INVALID_SCHEDULE", "Некорректная дата, выход или интервал рейса") from None
            rows.append(
                {
                    "route": row["route"],
                    "service_date": str(day),
                    "direction": row["direction"],
                    "start_minute": start,
                    "end_minute": end,
                    "duty": row["grafic"],
                    "trip": row["trip_num"],
                    "garage": row.get("garage_number", ""),
                }
            )
        if not rows:
            raise DomainError("INVALID_SCHEDULE", "Расписание пусто")
        result = pd.DataFrame(rows)
        keys = ["route", "service_date", "duty", "trip"]
        if result.duplicated(keys).any():
            raise DomainError("SCHEDULE_CONFLICT", "Один рейс имеет несколько разных назначений")
        return result, {
            "method": "planned_duty",
            "valid_from": result.service_date.min(),
            "valid_to": str(date.fromisoformat(result.service_date.max()) + timedelta(days=1)),
            "covered_route_dates": result[["route", "service_date"]].drop_duplicates().values.tolist(),
            "incomplete_route_weekdays": [],
            "assumptions": [
                "Включено время рейсов без межрейсового отстоя; полнота наряда заявлена источником"
            ],
            "assignment_coverage": float(result.garage.ne("").mean()),
        }

    def hours(self, ident, time_range: TimeRange, routes, *, scenario=False, direction=None):
        manifest = self.store.read("schedules", ident)
        trips = schedule_table(self.store.root, ident)
        trips = trips.loc[trips.route.isin(routes)]
        if direction:
            trips = trips.loc[trips.direction == direction]
        intervals, covered = [], set()
        start_day = time_range.start.date() - timedelta(days=1)
        end_day = time_range.end.date()
        incomplete = {tuple(pair) for pair in manifest["incomplete_route_weekdays"]}
        for day in (start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)):
            applicable = manifest["valid_from"] <= str(day) < manifest["valid_to"]
            if manifest["method"] == "stop_timetable_estimate":
                selected = (
                    trips.loc[trips.weekday == day.weekday()] if applicable or scenario else trips.iloc[:0]
                )
                for route in selected.route.unique():
                    if (route, day.weekday()) not in incomplete:
                        covered.add((route, day))
            else:
                selected = trips.loc[trips.service_date == str(day)] if applicable else trips.iloc[:0]
                covered.update((route, day) for route in selected.route.unique())
            midnight = datetime.combine(day, datetime.min.time(), TZ)
            for row in selected.to_dict("records"):
                garage = normal_id(row["garage"])
                resource = (
                    "garage:" + garage
                    if garage
                    else f"duty:{row['route']}:{day}:{normal_id(row['duty'])}"
                    if row["duty"]
                    else f"trip:{row['route']}:{day}:{row['direction']}:{row['trip']}"
                )
                intervals.append(
                    {
                        "route": row["route"],
                        "resource": resource,
                        "start": midnight + timedelta(minutes=row["start_minute"]),
                        "end": midnight + timedelta(minutes=row["end_minute"]),
                    }
                )
        integrated, conflicts = integrate(intervals, time_range)
        result = []
        for route in routes:
            for stamp in pd.date_range(time_range.start, time_range.end, freq="h", inclusive="left"):
                service_day = (
                    stamp.date() - timedelta(days=1)
                    if manifest["method"] == "stop_timetable_estimate" and stamp.hour < 3
                    else stamp.date()
                )
                known = (
                    (route, service_day) in covered
                    or (manifest["method"] == "planned_duty" and (route, stamp) in integrated)
                ) and route not in conflicts
                result.append(
                    {
                        "route": route,
                        "timestamp": stamp.isoformat(),
                        "vehicle_hours": integrated.get((route, stamp), 0.0) if known else None,
                        "method": manifest["method"] if known else "missing",
                        "schedule_id": ident,
                        "scenario": bool(scenario),
                        "quality_flags": ["vehicle_assignment_conflict"]
                        if route in conflicts
                        else []
                        if known
                        else ["schedule_not_applicable_or_incomplete"],
                    }
                )
        return result

    def reconcile(self, ident, events):
        manifest = self.store.read("schedules", ident)
        if manifest["method"] != "planned_duty":
            return {
                "schedule_id": ident,
                "matched_events": None,
                "reason": "В расписании нет номеров выходов и назначений вагонов",
            }
        trips = schedule_table(self.store.root, ident)
        assignments = {}
        for row in trips.itertuples():
            midnight = datetime.combine(date.fromisoformat(row.service_date), datetime.min.time(), TZ)
            key = row.route, row.service_date, normal_id(row.duty)
            assignments.setdefault(key, []).append(
                (
                    midnight + timedelta(minutes=row.start_minute),
                    midnight + timedelta(minutes=row.end_minute),
                    normal_id(row.garage),
                )
            )
        total = matched = conflicts = 0
        for batch in events:
            for row in batch.itertuples():
                total += 1
                spans = []
                for offset in (0, 1):
                    key = (
                        row.route,
                        str(row.event_time.date() - timedelta(days=offset)),
                        normal_id(row.bus_exit_no),
                    )
                    spans.extend(
                        span for span in assignments.get(key, []) if span[0] <= row.event_time < span[1]
                    )
                if spans:
                    matched += 1
                    garages = {garage for _, _, garage in spans if garage}
                    if garages and normal_id(row.garage_number) not in garages:
                        conflicts += 1
        return {
            "schedule_id": ident,
            "events": total,
            "matched_events": matched,
            "garage_conflicts": conflicts,
            "coverage": matched / total if total else None,
            "method": "route_service_date_duty",
            "execution_probability": None,
        }
