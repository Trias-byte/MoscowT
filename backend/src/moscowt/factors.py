"""Frozen hourly weather and incident observations. Network I/O is confined to ingestion."""

import io
import json
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
import pandas as pd

from .constants.factors import (
    ACCIDENT_URL as ACCIDENT_URL,
    WEATHER_COLUMNS as WEATHER_COLUMNS,
)
from .domain import DomainError
from .storage import SnapshotStore, atomic_write, digest, file_hash


class FactorRepository:
    def __init__(self, root):
        self.store = SnapshotStore(Path(root))

    def list(self, kind):
        return [json.loads(p.read_bytes()) for p in sorted((self.store.root / kind).glob("*.json"))]

    def save(self, kind, frame, provenance, raw=None):
        ident = kind + "-" + digest(provenance)
        existing = self.store.path(kind, ident)
        if existing.exists():
            return self.store.read(kind, ident)
        parquet = io.BytesIO()
        frame.to_parquet(parquet, index=False)
        path = self.store.path(kind, ident, "parquet")
        atomic_write(path, parquet.getvalue())
        atomic_write(self.store.path(kind, ident, "csv"), frame.to_csv(index=False).encode("utf-8-sig"))
        if raw is not None:
            atomic_write(self.store.path(kind, ident, "raw"), raw)
        manifest = {
            "id": ident,
            **provenance,
            "rows": len(frame),
            "sha256": file_hash(path),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "start": frame.timestamp.min().isoformat(),
            "end": frame.timestamp.max().isoformat(),
            "timezone": "Europe/Moscow",
        }
        self.store.put(kind, ident, manifest)
        return manifest

    def read(self, kind, ident):
        manifest = self.store.read(kind, ident)
        path = self.store.path(kind, ident, "parquet")
        if file_hash(path) != manifest["sha256"]:
            raise DomainError("FACTOR_CORRUPTED", "Контрольная сумма внешних данных не совпадает")
        return manifest, pd.read_parquet(path)

    def fetch_weather(self, start="2024-01-01", end="2025-12-31"):
        frames, requests, responses = [], [], []
        for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
            query = {
                "latitude": 55.75,
                "longitude": 37.62,
                "hourly": ",".join(WEATHER_COLUMNS),
                "timezone": "Europe/Moscow",
                "models": "era5",
                "start_date": max(start, f"{year}-01-01"),
                "end_date": min(end, f"{year}-12-31"),
            }
            url = "https://archive-api.open-meteo.com/v1/archive?" + urlencode(query)
            with urlopen(url, timeout=90) as response:
                payload = json.load(response)
            frame = pd.DataFrame(payload["hourly"]).rename(columns={"time": "timestamp"})
            frame.timestamp = pd.to_datetime(frame.timestamp).dt.tz_localize("Europe/Moscow")
            frames.append(frame)
            requests.append(url)
            responses.append(payload)
        frame = pd.concat(frames, ignore_index=True)
        if frame.timestamp.duplicated().any() or not np.isfinite(frame[list(WEATHER_COLUMNS)]).all().all():
            raise DomainError("WEATHER_INCOMPLETE", "Неполный почасовой архив погоды")
        return self.save(
            "weather_hourly",
            frame,
            {
                "requests": requests,
                "source": "https://open-meteo.com/en/docs/historical-weather-api",
                "license": "CC BY 4.0; ERA5 Copernicus terms; free noncommercial API",
                "coverage": "Moscow grid cell 55.75,37.62",
                "schema": 1,
                "availability": "retrospective reanalysis; not a historical forecast release",
                "content_digest": digest(responses),
            },
            json.dumps(responses).encode(),
        )

    def fetch_accidents(self):
        with urlopen(ACCIDENT_URL, timeout=90) as response:
            raw = response.read()
        return self.import_accidents(raw)

    def import_accidents(self, raw):
        """Normalize the frozen original archive without any network requests."""
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document = json.loads(archive.read("moskva.geojson"))
        records = []
        for feature in document["features"]:
            p = feature["properties"]
            if not str(p["datetime"]).startswith("2025-"):
                continue
            point = p.get("point") or {}
            records.append(
                {
                    "id": str(p["id"]),
                    "timestamp": p["datetime"],
                    "latitude": point.get("lat"),
                    "longitude": point.get("long"),
                    "category": p.get("category"),
                    "severity": p.get("severity"),
                    "injured_count": p.get("injured_count"),
                    "dead_count": p.get("dead_count"),
                    "published_at": None,
                }
            )
        frame = pd.DataFrame(records).drop_duplicates("id").sort_values(["timestamp", "id"])
        frame.timestamp = pd.to_datetime(frame.timestamp).dt.tz_localize("Europe/Moscow")
        frame["coordinate_valid"] = frame.latitude.between(54.9, 56.2) & frame.longitude.between(36.5, 38.3)
        import hashlib

        return self.save(
            "accidents",
            frame,
            {
                "name": "moscow_accidents_2025",
                "request": ACCIDENT_URL,
                "source": "https://dtp-stat.ru/opendata/",
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "license": "Использование материалов с активной ссылкой на https://dtp-stat.ru/",
                "coverage": "Москва, 2025; преимущественно ДТП с пострадавшими; не все задержки движения",
                "months": {str(k): int(v) for k, v in frame.groupby(frame.timestamp.dt.month).size().items()},
                "availability": "publication time unknown; retrospective diagnostics only",
                "schema": 1,
            },
            raw,
        )

    def link_accidents(self, ident, store, network_id, radius=100):
        from .network_history import resolve_network

        manifest, frame = self.read("accidents", ident)
        archive = store.read("networks", network_id)
        records, covered = [], {}
        for day, group in frame.loc[frame.coordinate_valid].groupby(frame.timestamp.dt.strftime("%Y-%m-%d")):
            network = resolve_network(store, archive, day)
            covered[day] = sorted({s["routeId"] for s in network["segments"]})
            for row in group.itertuples():
                matches = nearby_routes(network, row.longitude, row.latitude, radius)
                records.extend(
                    {
                        "id": row.id,
                        "timestamp": row.timestamp,
                        "route": r["route_id"],
                        "distance_m": r["distance_m"],
                    }
                    for r in matches
                )
        linked = pd.DataFrame(records, columns=["id", "timestamp", "route", "distance_m"])
        if linked.empty:
            raise DomainError("NO_INCIDENT_MATCHES", "Нет ДТП рядом с выбранной геометрией")
        return self.save(
            "accident_links",
            linked,
            {
                "accident_dataset_id": ident,
                "network_id": network_id,
                "radius_m": radius,
                "coverage_start": "2025-01-01",
                "coverage_end": "2026-01-01",
                "route_coverage": covered,
                "source_sha256": manifest["sha256"],
                "method": "proximity_candidate_not_confirmed_disruption",
            },
        )


def nearby_routes(network, longitude, latitude, radius=100):
    """Point-to-track distance in a local metric projection; not point-to-stop distance."""
    scale = np.array([111320 * np.cos(np.radians(latitude)), 111320])
    point = np.array([longitude, latitude])
    distances = {}
    for segment in network["segments"]:
        xy = (np.asarray(segment["coordinates"]) - point) * scale
        a, b = xy[:-1], xy[1:]
        delta = b - a
        denom = (delta * delta).sum(axis=1)
        t = np.clip(-(a * delta).sum(axis=1) / np.maximum(denom, 1e-12), 0, 1)
        distance = float(np.linalg.norm(a + t[:, None] * delta, axis=1).min()) if len(a) else float("inf")
        route = segment["routeId"]
        if distance <= radius and distance < distances.get(route, float("inf")):
            distances[route] = distance
    return [{"route_id": route, "distance_m": round(d, 1)} for route, d in sorted(distances.items())]


@lru_cache(maxsize=8)
def factor_frame(root, kind, ident):
    return FactorRepository(root).read(kind, ident)


def hourly_weather(root, ident, target, origin, observed=False, forecast_id=None):
    if forecast_id:
        from .external import OpenMeteoForecastProvider

        provider = OpenMeteoForecastProvider(root)
        release = provider.store.read("weather_forecasts", forecast_id)
        if pd.Timestamp(release["available_at"]) > origin:
            raise DomainError("WEATHER_NOT_AVAILABLE", "Погодный выпуск получен после origin")
        hourly = release["response"].get("hourly")
        if not hourly:
            raise DomainError("HOURLY_WEATHER_REQUIRED", "Выпуск не содержит почасовую погоду")
        stamps = pd.to_datetime(hourly["time"]).tz_localize("Europe/Moscow")
        covered = target.timestamp.isin(stamps) & target.timestamp.lt(
            pd.Timestamp(origin) + pd.Timedelta(days=1)
        )
        result = pd.DataFrame(index=range(len(target)), columns=WEATHER_COLUMNS, dtype=float)
        if covered.any():
            result.loc[covered.to_numpy(), :] = provider.select_hourly(
                forecast_id, target.loc[covered, "timestamp"], origin
            ).to_numpy()
        if (~covered).any():
            result.loc[(~covered).to_numpy(), :] = hourly_weather(
                root, ident, target.loc[~covered], origin, observed
            ).to_numpy()
        return result
    _, frame = factor_frame(root, "weather_hourly", ident)
    if observed:
        result = frame.set_index("timestamp")[list(WEATHER_COLUMNS)].reindex(target.timestamp)
    else:
        past = frame.loc[frame.timestamp.dt.year < pd.Timestamp(origin).year]
        # Only completed years enter the scenario baseline, including for backtests.
        climate = past.groupby([past.timestamp.dt.month, past.timestamp.dt.day, past.timestamp.dt.hour])[
            list(WEATHER_COLUMNS)
        ].mean()
        keys = pd.MultiIndex.from_arrays(
            [target.timestamp.dt.month, target.timestamp.dt.day, target.timestamp.dt.hour]
        )
        result = climate.reindex(keys)
        fallback = past.groupby([past.timestamp.dt.month, past.timestamp.dt.hour])[
            list(WEATHER_COLUMNS)
        ].mean()
        fallback_keys = pd.MultiIndex.from_arrays([target.timestamp.dt.month, target.timestamp.dt.hour])
        result = result.reset_index(drop=True).fillna(fallback.reindex(fallback_keys).reset_index(drop=True))
    if result.isna().any().any():
        raise DomainError(
            "WEATHER_COVERAGE_MISSING", "Нет погоды для выбранного периода или климатологии до origin"
        )
    return result.reset_index(drop=True)


def accident_features(root, ident, target, observed=False):
    manifest, linked = factor_frame(root, "accident_links", ident)
    # Unknown future incidents are absent; the archive gives no publication time.
    result = pd.DataFrame(
        {
            "accidents_1h": np.zeros(len(target)),
            "accidents_3h": np.zeros(len(target)),
            "accidents_archive_coverage": np.zeros(len(target)),
        }
    )
    if not observed:
        return result
    for route, positions in target.groupby("route").indices.items():
        rows = target.iloc[positions]
        coverage = manifest["route_coverage"]
        covered = np.array([route in coverage.get(day, []) for day in rows.timestamp.dt.strftime("%Y-%m-%d")])
        result.loc[positions, "accidents_archive_coverage"] = covered.astype(float)
        stamps = np.sort(linked.loc[linked.route.eq(route), "timestamp"].astype("int64").to_numpy())
        ends = (rows.timestamp + pd.Timedelta(hours=1)).astype("int64").to_numpy()
        for hours in (1, 3):
            counts = np.searchsorted(stamps, ends, side="left") - np.searchsorted(
                stamps, ends - hours * 3600 * 10**9, side="left"
            )
            # -1 is an explicit missing-feature sentinel, never an observed count or target.
            result.loc[positions, f"accidents_{hours}h"] = np.where(covered, counts, -1)
    return result
