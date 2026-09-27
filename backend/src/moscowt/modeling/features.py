"""Causal calendar and historical-analogue features, shared by fit and predict.

Each supervised block has a fixed origin. No value at or after that origin is
available to the feature builder, including when predicting a historical period.
"""

from datetime import timedelta

import numpy as np
import pandas as pd

FEATURE_VERSION = "fixed-origin-hourly-factors-v3"


class FeatureBuilder:
    def __init__(
        self,
        route_ids,
        external_snapshot_id=None,
        feature_groups=None,
        weather_hourly_id=None,
        accident_links_id=None,
    ):
        self.external_snapshot_id = external_snapshot_id
        self.feature_groups = ["calendar"] if feature_groups is None else feature_groups
        self.route_codes = {route: index for index, route in enumerate(sorted(route_ids))}
        self.weather_hourly_id = weather_hourly_id
        self.accident_links_id = accident_links_id

    def build(self, history, target, origin):
        import holidays

        from ..service_calendar import CALENDAR_AVAILABLE

        origin = pd.Timestamp(origin)
        past = history.loc[(history.timestamp < origin) & history.value.notna()].copy()
        calendar = {}
        for year in sorted(set(target.timestamp.dt.year)):
            known = year in CALENDAR_AVAILABLE and CALENDAR_AVAILABLE[year] <= origin.date()
            calendar.update(holidays.Russia(years=[year], observed=known))
        dt = target.timestamp.dt
        dates = dt.date
        holiday = dates.map(lambda day: day in calendar).astype(int)

        # Calendar runs are counted once per date, never once per route/hour.
        def streak(day):
            count = 0
            while day in calendar:
                count += 1
                day -= timedelta(days=1)
            return count

        def after(day):
            for offset in range(1, 8):
                if day - timedelta(days=offset) in calendar:
                    return offset
            return 0

        day_features = {day: (streak(day), after(day)) for day in dates.unique()}
        result = pd.DataFrame(
            {
                "route": target.route.map(self.route_codes),
                "hour": dt.hour,
                "dow": dt.dayofweek,
                "month": dt.month,
                "week_of_year": dt.isocalendar().week.to_numpy(dtype=int),
                "is_weekend": (dt.dayofweek >= 5).astype(int),
                "is_saturday": (dt.dayofweek == 5).astype(int),
                "is_sunday": (dt.dayofweek == 6).astype(int),
                "is_holiday": holiday,
                "is_new_year_holidays": ((dt.month == 1) & (dt.day <= 8)).astype(int),
                "is_december": (dt.month == 12).astype(int),
                "is_november": (dt.month == 11).astype(int),
                # Stable calendar proxy; school-specific vacation calendars are not available.
                "is_school_holiday": (dt.month.isin([6, 7, 8]) | ((dt.month == 1) & (dt.day <= 8))).astype(
                    int
                ),
                "is_night": dt.hour.isin([0, 1, 2, 3, 4, 5]).astype(int),
                "is_night_deep": dt.hour.isin([2, 3]).astype(int),
                "is_new_year_eve": ((dt.month == 12) & (dt.day == 31)).astype(int),
                "is_school_start": ((dt.month == 9) & (dt.day == 1)).astype(int),
                "holiday_streak": dates.map(lambda day: day_features[day][0]),
                "days_after_holiday": dates.map(lambda day: day_features[day][1]),
                "hour_sin": np.sin(2 * np.pi * dt.hour / 24),
                "hour_cos": np.cos(2 * np.pi * dt.hour / 24),
                "dow_sin": np.sin(2 * np.pi * dt.dayofweek / 7),
                "dow_cos": np.cos(2 * np.pi * dt.dayofweek / 7),
            }
        ).reset_index(drop=True)
        lookup_rows = []
        for route, rows in past.groupby("route", sort=False):
            series = rows.set_index("timestamp").value.sort_index()
            recent = series[series.index >= origin - pd.Timedelta(days=56)]
            profile = recent.groupby([recent.index.dayofweek, recent.index.hour]).mean()
            route_mean = series[series.index >= origin - pd.Timedelta(days=30)].mean()
            if not np.isfinite(route_mean):
                route_mean = series.mean()
            rolling = {
                days: (
                    series.rolling(f"{days}D", closed="left").mean(),
                    series.rolling(f"{days}D", closed="left").std(ddof=0),
                )
                for days in (7, 14, 28)
            }
            references = rows.groupby([rows.timestamp.dt.dayofweek, rows.timestamp.dt.hour]).timestamp.max()
            for (dow, hour), reference in references.items():
                fallback = float(profile.get((dow, hour), route_mean))
                row = {"route_id": route, "dow_key": dow, "hour_key": hour, "route_mean_30": route_mean}
                for lag in (24, 168, 336, 672):
                    row[f"lag_{lag}h"] = series.get(reference - pd.Timedelta(hours=lag), fallback)
                for days, (means, stds) in rolling.items():
                    row[f"roll_mean_{days}"] = means.get(reference, fallback)
                    row[f"roll_std_{days}"] = stds.get(reference, 0.0)
                lookup_rows.append(row)
        lookup = pd.DataFrame(lookup_rows).set_index(["route_id", "dow_key", "hour_key"])
        keys = pd.MultiIndex.from_arrays([target.route, dt.dayofweek, dt.hour])
        lag_frame = lookup.reindex(keys).reset_index(drop=True)
        features = pd.concat([result, lag_frame], axis=1).astype(float)
        # No authoritative school-specific calendar is available.
        features["is_school_holiday"] = 0.0
        if "calendar" not in self.feature_groups:
            features = features.drop(
                columns=[
                    "month",
                    "week_of_year",
                    "is_holiday",
                    "is_new_year_holidays",
                    "is_december",
                    "is_november",
                    "is_school_holiday",
                    "is_new_year_eve",
                    "is_school_start",
                    "holiday_streak",
                    "days_after_holiday",
                ]
            )
        if self.external_snapshot_id:
            from ..external import external_features

            extras = external_features(
                history.attrs.get("external_root"),
                self.external_snapshot_id,
                target,
                origin,
                [
                    g
                    for g in self.feature_groups
                    if g != "weather" or not getattr(self, "weather_hourly_id", None)
                ],
                history.attrs.get("weather_forecast_id"),
            )
            features = pd.concat([features, extras], axis=1)
        if "weather" in self.feature_groups and getattr(self, "weather_hourly_id", None):
            from ..factors import hourly_weather

            weather = hourly_weather(
                history.attrs["external_root"],
                self.weather_hourly_id,
                target,
                origin,
                history.attrs.get("observed_factors", False),
            )
            if history.attrs.get("weather_forecast_id"):
                from ..external import OpenMeteoForecastProvider

                weather = OpenMeteoForecastProvider(history.attrs["external_root"]).select_hourly(
                    history.attrs["weather_forecast_id"], target.timestamp, origin
                )
            overrides = history.attrs.get("factor_overrides")
            if overrides:
                mask = (
                    target.route.isin(overrides["route_ids"])
                    & target.timestamp.ge(overrides["start"])
                    & target.timestamp.lt(overrides["end"])
                )
                for column, value in overrides.get("weather", {}).items():
                    if value is not None:
                        weather.loc[mask, column] = value
            features = pd.concat([features, weather.add_prefix("weather_")], axis=1)
        if "accidents" in self.feature_groups and getattr(self, "accident_links_id", None):
            from ..factors import accident_features

            features = pd.concat(
                [
                    features,
                    accident_features(
                        history.attrs["external_root"],
                        self.accident_links_id,
                        target,
                        history.attrs.get("observed_factors", False),
                    ),
                ],
                axis=1,
            )
        return features

    def supervised(self, history):
        history = history.copy()
        history.attrs["observed_factors"] = True
        first = history.timestamp.min().normalize() + pd.Timedelta(days=28)
        end = history.timestamp.max() + pd.Timedelta(hours=1)
        blocks, labels, weights = [], [], []
        for origin in pd.date_range(first, end, freq="7D", inclusive="left"):
            target = history.loc[
                (history.timestamp >= origin)
                & (history.timestamp < origin + pd.Timedelta(days=7))
                & history.value.notna()
            ].reset_index(drop=True)
            if target.empty:
                continue
            blocks.append(self.build(history, target[["route", "timestamp"]], origin))
            labels.append(target.value.to_numpy())
            weights.append(np.where(target.timestamp.dt.hour.isin([2, 3]), 0.3, 1.0))
        if not blocks:
            raise ValueError("Not enough history for causal training blocks")
        return pd.concat(blocks, ignore_index=True), np.concatenate(labels), np.concatenate(weights)
