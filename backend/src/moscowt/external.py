"""Frozen external inputs and causal feature access. No HTTP calls on prediction paths."""

import io
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
import pandas as pd

from .constants.external import (
    EVENTS as EVENTS,
    SOURCES as SOURCES,
    WEATHER_FIELDS as WEATHER_FIELDS,
)
from .domain import DomainError
from .service_calendar import is_workday
from .storage import SnapshotStore, atomic_write, digest, file_hash


class OpenMeteoForecastProvider:
    """Freeze the response before using it. Receipt time is a conservative availability bound."""

    def __init__(self, root):
        self.store = SnapshotStore(Path(root))

    def fetch(self):
        query = {
            "latitude": 55.75,
            "longitude": 37.62,
            "daily": ",".join(WEATHER_FIELDS),
            "hourly": "temperature_2m,relative_humidity_2m,precipitation",
            "timezone": "Europe/Moscow",
            "wind_speed_unit": "ms",
            "forecast_days": 7,
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urlencode(query)
        with urlopen(url, timeout=30) as response:
            payload = json.load(response)
        return self.register(payload, datetime.now(timezone.utc), url)

    def register(self, payload, received_at, url):
        frame = pd.DataFrame(payload["daily"]).rename(columns={"time": "date"})
        frame.date = pd.to_datetime(frame.date)
        if (
            frame.empty
            or frame.date.duplicated().any()
            or not np.isfinite(frame[list(WEATHER_FIELDS)]).all().all()
        ):
            raise DomainError("INVALID_EXTERNAL_DATA", "Неполный погодный выпуск")
        if pd.Timestamp(received_at).tzinfo is None:
            raise DomainError("INVALID_EXTERNAL_DATA", "Время получения должно содержать часовой пояс")
        identity = {
            "response": payload,
            "available_at": pd.Timestamp(received_at).isoformat(),
            "query_url": url,
        }
        ident = "weather-" + digest(identity)
        manifest = {
            "id": ident,
            **identity,
            "start": str(frame.date.min().date()),
            "end": str((frame.date.max() + pd.Timedelta(days=1)).date()),
            "license": "CC BY 4.0; free API for noncommercial use",
            "coverage": "One Moscow grid cell, requested 55.75, 37.62",
            "publication_time": None,
            "availability_policy": "observed_receipt_time",
            "note": "Receipt time, not model initialization; this is not a historical hindcast",
        }
        self.store.put("weather_forecasts", ident, manifest)
        return manifest

    def list(self):
        return [
            json.loads(p.read_bytes()) for p in sorted((self.store.root / "weather_forecasts").glob("*.json"))
        ]

    def select(self, ident, dates, origin):
        manifest = self.store.read("weather_forecasts", ident)
        if pd.Timestamp(manifest["available_at"]) > origin:
            raise DomainError("WEATHER_NOT_AVAILABLE", "Погодный выпуск получен после origin")
        frame = pd.DataFrame(manifest["response"]["daily"]).set_index("time")
        selected = frame.reindex(dates.dt.strftime("%Y-%m-%d"))[list(WEATHER_FIELDS)].reset_index(drop=True)
        if selected.isna().any().any():
            raise DomainError("EXTERNAL_COVERAGE_MISSING", "Погодный выпуск не покрывает выбранный день")
        return selected

    def select_hourly(self, ident, dates, origin):
        from .factors import WEATHER_COLUMNS

        manifest = self.store.read("weather_forecasts", ident)
        if pd.Timestamp(manifest["available_at"]) > origin:
            raise DomainError("WEATHER_NOT_AVAILABLE", "Погодный выпуск получен после origin")
        if "hourly" not in manifest["response"]:
            raise DomainError("HOURLY_WEATHER_REQUIRED", "Этот выпуск не содержит почасовую влажность")
        frame = pd.DataFrame(manifest["response"]["hourly"])
        frame.index = pd.to_datetime(frame.pop("time")).dt.tz_localize("Europe/Moscow")
        selected = frame.reindex(dates)[list(WEATHER_COLUMNS)].reset_index(drop=True)
        if not np.isfinite(selected).all().all():
            raise DomainError("EXTERNAL_COVERAGE_MISSING", "Погодный выпуск не покрывает выбранные часы")
        return selected


class ExternalRepository:
    def __init__(self, root):
        self.store = SnapshotStore(Path(root))

    def list(self):
        return [json.loads(p.read_bytes()) for p in sorted((self.store.root / "external").glob("*.json"))]

    def register(self, weather_directory: Path):
        path = weather_directory / "weather_history_2014_2025_09.parquet"
        metadata = json.loads((weather_directory / "sources.json").read_bytes())
        frame = pd.read_parquet(path)
        required = ["date", "temperature_2m_mean", "precipitation_sum", "snowfall_sum", "wind_speed_10m_mean"]
        frame = frame[required].copy()
        frame.date = pd.to_datetime(frame.date)
        if frame.date.duplicated().any() or not np.isfinite(frame[required[1:]].to_numpy()).all():
            raise DomainError("INVALID_EXTERNAL_DATA", "Некорректный погодный архив")
        identity = {
            "source_sha256": file_hash(path),
            "metadata": metadata,
            "events": EVENTS,
            "sources": SOURCES,
            "schema": 1,
        }
        ident = "external-" + digest(identity)
        buffer = io.BytesIO()
        frame.to_parquet(buffer, index=False)
        target = self.store.path("external", ident, "parquet")
        atomic_write(target, buffer.getvalue())
        result = {
            "id": ident,
            **identity,
            "sha256": file_hash(target),
            "start": str(frame.date.min().date()),
            "end": str(frame.date.max().date()),
            "rows": len(frame),
            "availability_policy": "retrospective_reanalysis_vintage; prior-completed-year climatology only",
            "events_note": "Publication is conservatively available from next midnight; persistent regime end is a scenario assumption, not an operating closure",
        }
        self.store.put("external", ident, result)
        return result

    def frame(self, ident):
        manifest = self.store.read("external", ident)
        path = self.store.path("external", ident, "parquet")
        if file_hash(path) != manifest["sha256"]:
            raise DomainError("EXTERNAL_CORRUPTED", "Контрольная сумма внешних данных не совпадает", 503)
        return manifest, pd.read_parquet(path)


@lru_cache(maxsize=8)
def weather_archive(root, ident):
    return ExternalRepository(root).frame(ident)


def external_features(root, ident, target, origin, groups, weather_forecast_id=None):
    origin = pd.Timestamp(origin)
    result = pd.DataFrame(index=target.index)
    if "calendar" in groups:
        result["civil_day_off"] = target.timestamp.dt.date.map(lambda day: int(not is_workday(day)))
        # Future calendars not officially published before origin remain an ordinary weekday/weekend proxy.
        published = {
            2025: pd.Timestamp("2024-10-05", tz="Europe/Moscow"),
            2026: pd.Timestamp("2025-09-25", tz="Europe/Moscow"),
        }
        known = target.timestamp.dt.year.map(lambda year: year in published and published[year] < origin)
        result.loc[~known, "civil_day_off"] = (target.loc[~known].timestamp.dt.dayofweek >= 5).astype(int)
    if set(groups) & {"weather", "events"}:
        if not ident or not root:
            raise DomainError("EXTERNAL_DATA_REQUIRED", "Выберите версию внешних данных")
        manifest, weather = weather_archive(str(root), ident)
        if "weather" in groups:
            # Entire reference years must end before origin; future observed weather cannot enter.
            past = weather.loc[weather.date.dt.year < origin.year]
            fields = [c for c in past.columns if c != "date"]
            climate = past.groupby([past.date.dt.month, past.date.dt.day])[fields].mean()
            keys = pd.MultiIndex.from_arrays([target.timestamp.dt.month, target.timestamp.dt.day])
            selected = climate.reindex(keys).reset_index(drop=True)
            leap = target.timestamp.dt.month.eq(2) & target.timestamp.dt.day.eq(29)
            if leap.any() and (2, 29) not in climate.index:
                selected.loc[leap] = (climate.loc[(2, 28)] + climate.loc[(3, 1)]).to_numpy() / 2
            if selected.isna().any().any():
                raise DomainError("EXTERNAL_COVERAGE_MISSING", "Неполная климатология до origin")
            if weather_forecast_id:
                provider = OpenMeteoForecastProvider(root)
                release = provider.store.read("weather_forecasts", weather_forecast_id)
                if pd.Timestamp(release["available_at"]) > origin:
                    raise DomainError("WEATHER_NOT_AVAILABLE", "Погодный выпуск получен после origin")
                covered = target.timestamp.dt.strftime("%Y-%m-%d").isin(release["response"]["daily"]["time"])
                covered &= target.timestamp.lt(origin + pd.Timedelta(days=1))
                if covered.any():
                    selected.loc[covered.to_numpy(), fields] = provider.select(
                        weather_forecast_id, target.loc[covered, "timestamp"], origin
                    )[fields].to_numpy()
            for column in fields:
                # Stable schema for stored models; a selected daily release replaces its climate baseline.
                result["climate_" + column] = selected[column].to_numpy()
        if "events" in groups:
            result["known_service_change"] = 0.0
            dates = target.timestamp.dt.strftime("%Y-%m-%d")
            for event in manifest["events"]:
                if pd.Timestamp(event["published_at"]) >= origin:
                    continue
                selected = (
                    target.route.isin(event["route_ids"]) & dates.ge(event["start"]) & dates.lt(event["end"])
                )
                result.loc[selected, "known_service_change"] = 1.0
    return result
