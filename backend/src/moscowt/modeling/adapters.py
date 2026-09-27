import numpy as np
import pandas as pd
from pydantic import Field

from ..domain import TZ, DomainError, StrictModel
from .competition import CompetitionAdapter
from .contracts import ModelAdapter
from .features import FEATURE_VERSION, FeatureBuilder


class EnsembleParameters(StrictModel):
    lgb_estimators: int = Field(default=332, ge=1, le=4000)
    cat_iterations: int = Field(default=1378, ge=1, le=4000)
    rf_estimators: int = Field(default=300, ge=1, le=1000)
    depth: int = Field(default=8, ge=2, le=10)


class SeasonalAdapter(ModelAdapter):
    name, version, minimum_days = "seasonal", "weekday-hour-8w-v2", 21

    def fit(self, history, spec, threads):
        if spec.parameters:
            raise DomainError("INVALID_MODEL_PARAMETERS", "У сезонной модели нет настраиваемых параметров")
        return {"routes": spec.route_ids}

    def predict(self, model, history, spec):
        past = history.loc[
            (history.timestamp < spec.origin) & (history.timestamp >= spec.origin - pd.Timedelta(days=56))
        ]
        profile = past.groupby(["route", past.timestamp.dt.dayofweek, past.timestamp.dt.hour]).value.agg(
            ["mean", "count"]
        )
        target = target_grid(spec)
        keys = pd.MultiIndex.from_arrays(
            [target.route, target.timestamp.dt.dayofweek, target.timestamp.dt.hour]
        )
        selected = profile.reindex(keys)
        values = selected["mean"].to_numpy()
        if not np.isfinite(values).all() or (selected["count"].fillna(0) < 3).any():
            raise DomainError(
                "INSUFFICIENT_HISTORY", "Нужны хотя бы три наблюдения для каждого дня недели и часа до origin"
            )
        return target.assign(value=values)


class EnsembleAdapter(ModelAdapter):
    name, version, minimum_days = "lgb_cb_rf", "lgb-cb-rf-" + FEATURE_VERSION, 56

    def fit(self, history, spec, threads):
        from catboost import CatBoostRegressor
        from lightgbm import LGBMRegressor
        from sklearn.ensemble import RandomForestRegressor
        from threadpoolctl import threadpool_limits

        parameters = EnsembleParameters.model_validate(spec.parameters)
        builder = FeatureBuilder(
            spec.route_ids,
            spec.external_snapshot_id,
            spec.feature_groups,
            spec.weather_hourly_id,
            spec.accident_links_id,
        )
        features, labels, weights = builder.supervised(history)
        models = [
            (
                0.7,
                CatBoostRegressor(
                    iterations=parameters.cat_iterations,
                    learning_rate=0.03,
                    depth=parameters.depth,
                    l2_leaf_reg=3,
                    loss_function="MAE",
                    random_seed=spec.seed,
                    thread_count=threads,
                    verbose=False,
                    allow_writing_files=False,
                ),
            ),
            (
                0.1,
                LGBMRegressor(
                    n_estimators=parameters.lgb_estimators,
                    learning_rate=0.03,
                    num_leaves=63,
                    min_child_samples=20,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    reg_alpha=0.1,
                    reg_lambda=0.1,
                    objective="regression",
                    random_state=spec.seed,
                    n_jobs=threads,
                    verbosity=-1,
                ),
            ),
            (
                0.2,
                RandomForestRegressor(
                    n_estimators=parameters.rf_estimators,
                    max_depth=20,
                    min_samples_leaf=5,
                    max_features=0.5,
                    n_jobs=threads,
                    random_state=spec.seed,
                ),
            ),
        ]
        with threadpool_limits(limits=threads):
            for _, model in models:
                model.fit(features, labels, sample_weight=weights)
        return {
            "builder": builder,
            "models": models,
            "features": features.columns.tolist(),
            "training_rows": len(labels),
            "parameters": parameters.model_dump(),
            "feature_ranges": {
                name: {"min": float(features[name].min()), "max": float(features[name].max())}
                for name in features
                if name.startswith("weather_")
            },
        }

    def predict(self, model, history, spec):
        target = target_grid(spec)
        features = model["builder"].build(history, target, spec.origin)
        if features.columns.tolist() != model["features"]:
            raise DomainError("FEATURE_SCHEMA_MISMATCH", "Схема признаков не соответствует модели")
        # RF accumulates parallel tree results in completion order. A single
        # prediction thread makes exported floating-point values reproducible.
        values = np.zeros(len(target))
        for weight, estimator in model["models"]:
            if estimator.__class__.__name__ == "RandomForestRegressor":
                estimator.set_params(n_jobs=1)
            values += weight * estimator.predict(features)
        return target.assign(value=np.maximum(0, values))


class AnnualScenarioAdapter(SeasonalAdapter):
    name, version, minimum_days = "annual_scenario", "calendar-week-profile-scenario-v1", 56

    def predict(self, model, history, spec):
        from ..service_calendar import profile_weekday

        past = history.loc[
            (history.timestamp < spec.origin) & (history.timestamp >= spec.origin - pd.Timedelta(days=56))
        ]
        profile = past.groupby(["route", past.timestamp.dt.dayofweek, past.timestamp.dt.hour]).value.mean()
        target = target_grid(spec)
        days = target.timestamp.dt.date.map(lambda day: profile_weekday(day, spec.origin))
        keys = pd.MultiIndex.from_arrays([target.route, days, target.timestamp.dt.hour])
        values = profile.reindex(keys).to_numpy()
        if not np.isfinite(values).all():
            raise DomainError("INSUFFICIENT_HISTORY", "Неполный недельный профиль годового сценария")
        return target.assign(value=values)


def target_grid(spec):
    times = pd.date_range(
        pd.Timestamp(spec.time_range.start).tz_convert(TZ),
        pd.Timestamp(spec.time_range.end).tz_convert(TZ),
        freq="h",
        inclusive="left",
    )
    return pd.MultiIndex.from_product([sorted(spec.route_ids), times], names=["route", "timestamp"]).to_frame(
        index=False
    )


ADAPTERS = {
    adapter.name: adapter
    for adapter in (SeasonalAdapter(), EnsembleAdapter(), AnnualScenarioAdapter(), CompetitionAdapter())
}
