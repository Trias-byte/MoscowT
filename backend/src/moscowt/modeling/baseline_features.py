"""Versioned port of the 34 features used by ML's 0.88 submission.

Calendar/profile assumptions and float32 conversion are part of the model contract.
No research-directory imports or external weather downloads are needed at runtime.
"""

import hashlib

import numpy as np
import pandas as pd

from ..constants.modeling_baseline_features import (
    CAL as CAL,
    FEATURES as FEATURES,
    HISTORY as HISTORY,
    SPARSE as SPARSE,
)
from ..domain import TZ, DomainError
from ..service_calendar import CALENDAR_AVAILABLE, EXTRA_DAYS_OFF, WORKING_WEEKENDS


def history_fingerprint(history):
    """Content identity independent of ingestion batches and dataset IDs."""
    frame = history[["route", "timestamp", "value"]].sort_values(["route", "timestamp"]).copy()
    frame["timestamp"] = frame.timestamp.dt.tz_convert(TZ).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return hashlib.sha256(
        frame.to_csv(index=False, float_format="%.17g", lineterminator="\n").encode()
    ).hexdigest()


def calendar(dates, origin=None):
    dates = pd.DatetimeIndex(dates)

    def available(d):
        return origin is None or (
            d.year in CALENDAR_AVAILABLE and CALENDAR_AVAILABLE[d.year] <= origin.date()
        )

    holiday = np.array([d.date() in EXTRA_DAYS_OFF and available(d) for d in dates])
    working = np.array([d.date() in WORKING_WEEKENDS and available(d) for d in dates])
    return pd.DataFrame(
        {
            "date": dates,
            "dow": dates.dayofweek,
            "month": dates.month,
            "is_day_off": (((dates.dayofweek >= 5) | holiday) & ~working).astype(int),
            "is_holiday": holiday.astype(int),
            "is_working_weekend": working.astype(int),
            "profile_dow": np.where(working, 4, np.where(holiday, 6, dates.dayofweek)),
            "year_sin": np.sin(2 * np.pi * (dates.dayofyear - 1) / 365),
            "year_cos": np.cos(2 * np.pi * (dates.dayofyear - 1) / 365),
        }
    )


class BaselineFeatures:
    def __init__(self, history, routes, start, origin):
        self.start = pd.Timestamp(start).tz_convert(TZ).tz_localize(None)
        self.origin = pd.Timestamp(origin).tz_convert(TZ).tz_localize(None)
        if self.start != self.start.normalize() or self.origin != self.origin.normalize():
            raise DomainError(
                "DAILY_ORIGIN_REQUIRED", "База CatBoost требует начало истории и выпуск в 00:00 МСК"
            )
        self.routes = sorted(routes, key=lambda r: (0, int(r)) if r.isdigit() else (1, r))
        self.dates = pd.date_range(self.start, self.origin, inclusive="left")
        if len(self.dates) < 28:
            raise DomainError("INSUFFICIENT_HISTORY", "Нужны 28 полных дней истории до выпуска")
        past = history.loc[(history.timestamp >= start) & (history.timestamp < origin)].copy()
        local = past.timestamp.dt.tz_convert(TZ).dt.tz_localize(None)
        past["date"], past["hour"] = local.dt.normalize(), local.dt.hour
        expected = pd.MultiIndex.from_product(
            [self.dates, self.routes, range(24)], names=["date", "route", "hour"]
        )
        indexed = past.set_index(["date", "route", "hour"])
        if indexed.index.has_duplicates or len(indexed) != len(expected):
            raise DomainError(
                "INCOMPLETE_BASELINE_HISTORY",
                "Нужна полная уникальная сетка маршрут × час; пропуски не равны нулям",
            )
        values = indexed.reindex(expected).value.to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise DomainError("INCOMPLETE_BASELINE_HISTORY", "Нельзя подменять неизвестные наблюдения нулями")
        self.values = values.reshape(len(self.dates), len(self.routes), 24)
        self.cal = calendar(pd.date_range(self.start, self.origin + pd.Timedelta(days=60)), self.origin)
        self.snapshots = {}

    def snapshot(self, origin):
        if origin in self.snapshots:
            return self.snapshots[origin]
        if origin < 28 or origin > len(self.values):
            raise ValueError("Origin needs 28 completed historical days")
        past = self.values[:origin]
        hist_dow = self.cal.profile_dow.to_numpy()[:origin]
        snap = {}
        for w in [7, 14, 28, 56]:
            data = past[-w:]
            dows = hist_dow[-w:]
            hours = data.mean(axis=0)
            totals = data.sum(axis=2)
            snap[f"route_hour_{w}"] = np.broadcast_to(hours, (7, len(self.routes), 24)).copy()
            snap[f"day_mean_{w}"] = np.broadcast_to(
                totals.mean(axis=0)[None, :, None], (7, len(self.routes), 24)
            ).copy()
            for stat in ["profile"] + (["median"] if w in [28, 56] else []):
                a = np.empty((7, len(self.routes), 24))
                for d in range(7):
                    selected = data[dows == d]
                    a[d] = (
                        (np.median(selected, axis=0) if stat == "median" else selected.mean(axis=0))
                        if len(selected)
                        else hours
                    )
                snap[f"{stat}_{w}"] = a
            if w == 28:
                for name in ["day_dow_28", "profile_std_28", "profile_n_28"]:
                    a = np.empty((7, len(self.routes), 24))
                    for d in range(7):
                        selected = data[dows == d]
                        if name == "day_dow_28":
                            a[d] = (
                                selected.sum(axis=2).mean(axis=0) if len(selected) else totals.mean(axis=0)
                            )[:, None]
                        elif name == "profile_std_28":
                            a[d] = selected.std(axis=0) if len(selected) else data.std(axis=0)
                        else:
                            a[d] = len(selected)
                    snap[name] = a
        recent = past[-14:].sum(axis=(0, 2))
        previous = past[-28:-14].sum(axis=(0, 2))
        route_stats = {
            "trend_14": np.clip((recent + 1) / (previous + 1), 0.25, 4),
            "network_trend": np.repeat(
                np.clip((recent.sum() + 1) / (previous.sum() + 1), 0.25, 4), len(self.routes)
            ),
            "day_std_56": past[-56:].sum(axis=2).std(axis=0),
            "zero_fraction_28": (past[-28:] == 0).mean(axis=(0, 2)),
            "morning_fraction_28": past[-28:, :, 7:10].sum(axis=(0, 2)) / (past[-28:].sum(axis=(0, 2)) + 1),
            "evening_fraction_28": past[-28:, :, 16:20].sum(axis=(0, 2)) / (past[-28:].sum(axis=(0, 2)) + 1),
        }
        for name, a in route_stats.items():
            snap[name] = np.broadcast_to(a[None, :, None], (7, len(self.routes), 24)).copy()
        self.snapshots[origin] = snap
        return snap

    def build_pairs(self, pairs):
        origins, targets, horizons = np.asarray(pairs, dtype=int).T
        size = len(self.routes) * 24
        n = len(pairs)
        hour = np.tile(np.arange(24), n * len(self.routes))
        frame = pd.DataFrame(
            {
                "date": self.start + pd.to_timedelta(np.repeat(targets, size), unit="D"),
                "route": np.tile(np.repeat(self.routes, 24), n),
                "hour": hour,
                "horizon_days": np.repeat(horizons, size),
                "hour_sin": np.sin(2 * np.pi * hour / 24),
                "hour_cos": np.cos(2 * np.pi * hour / 24),
            }
        )
        for col in self.cal:
            if col != "date":
                frame[col] = np.repeat(self.cal[col].to_numpy()[targets], size)
        for col in HISTORY:
            frame[col] = np.concatenate(
                [self.snapshot(int(o))[col][int(self.cal.iloc[t].profile_dow)].ravel() for o, t, _ in pairs]
            )
        if targets.max() < len(self.values):
            frame["boardings"] = np.concatenate([self.values[t].ravel() for t in targets])
        for col in frame.select_dtypes("float"):
            frame[col] = frame[col].astype("float32")
        return frame

    def supervised(self):
        pairs = [
            (o, o + h - 1, h)
            for o in range(28, len(self.dates), 14)
            for h in SPARSE
            if o + h - 1 < len(self.dates)
        ]
        limit = 120_000 // (len(self.routes) * 24)
        if not pairs or limit < 1:
            raise DomainError("INSUFFICIENT_HISTORY", "Для обучения нужны хотя бы 29 полных дней")
        if len(pairs) > limit:
            rng = np.random.default_rng(2026)
            pairs = [pairs[i] for i in sorted(rng.choice(len(pairs), limit, replace=False))]
        return self.build_pairs(pairs)

    def forecast(self, spec):
        start = pd.Timestamp(spec.time_range.start).tz_convert(TZ).tz_localize(None)
        end = pd.Timestamp(spec.time_range.end).tz_convert(TZ).tz_localize(None)
        if end > self.origin + pd.Timedelta(days=61):
            raise DomainError("HORIZON_UNSUPPORTED", "База CatBoost поддерживает до 61 дня от выпуска")
        o = len(self.dates)
        days = pd.date_range(start.normalize(), (end - pd.Timedelta(hours=1)).normalize())
        pairs = [(o, (d - self.start).days, (d - self.origin).days + 1) for d in days]
        frame = self.build_pairs(pairs)
        frame["timestamp"] = (frame.date + pd.to_timedelta(frame.hour, unit="h")).dt.tz_localize(TZ)
        return (
            frame.loc[
                (frame.timestamp >= spec.time_range.start)
                & (frame.timestamp < spec.time_range.end)
                & frame.route.isin(spec.route_ids)
            ]
            .sort_values(["route", "timestamp"])
            .reset_index(drop=True)
        )

    def zero_routes(self):
        return [r for r, total in zip(self.routes, self.values.sum(axis=(0, 2))) if total == 0]
