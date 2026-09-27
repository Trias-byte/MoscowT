"""The five-seed CatBoost baseline that produced the submitted 61-day forecast."""

import numpy as np
from catboost import CatBoostRegressor

from ..constants.modeling_competition import (
    SEEDS as SEEDS,
)
from ..domain import DomainError
from .baseline_features import FEATURES, BaselineFeatures, history_fingerprint
from .contracts import ModelAdapter


class CompetitionAdapter(ModelAdapter):
    name, version, minimum_days = "competition_catboost", "cb-mae-sparse14-34f-5seed-v1", 29

    def capabilities(self):
        return {
            **super().capabilities(),
            "label": "Базовая · CatBoost × 5",
            "maximum_horizon_days": 61,
            "maximum_horizon": "61_days",
            "origin_hour_moscow": 0,
            "feature_groups": ["calendar"],
            "complete_history_required": True,
        }

    def fit(self, history, spec, threads):
        if spec.parameters or spec.seed != 42 or spec.feature_groups != ["calendar"]:
            raise DomainError(
                "INVALID_MODEL_PARAMETERS",
                "У базовой модели фиксированы параметры и seeds 42, 17, 123, 2025, 2026",
            )
        builder = BaselineFeatures(history, spec.route_ids, spec.time_range.start, spec.time_range.end)
        frame = builder.supervised()
        models = []
        for seed in SEEDS:
            estimator = CatBoostRegressor(
                iterations=450,
                depth=6,
                learning_rate=0.04,
                l2_leaf_reg=8,
                loss_function="MAE",
                random_seed=seed,
                thread_count=threads,
                verbose=False,
                allow_writing_files=False,
            )
            estimator.fit(frame[FEATURES], frame.boardings.to_numpy(dtype=float), cat_features=["route"])
            models.append((0.2, estimator))
        return {
            "models": models,
            "features": FEATURES,
            "routes": spec.route_ids,
            "start": spec.time_range.start.isoformat(),
            "zero_routes": builder.zero_routes(),
            "training_rows": len(frame),
            "seeds": SEEDS,
            "history_sha256": history_fingerprint(history),
        }

    def predict(self, model, history, spec):
        if model["features"] != FEATURES:
            raise DomainError("FEATURE_SCHEMA_MISMATCH", "Схема базовой модели не совпадает")
        builder = BaselineFeatures(history, model["routes"], model["start"], spec.origin)
        target = builder.forecast(spec)
        values = np.zeros(len(target))
        for weight, estimator in model["models"]:
            # Clip each member before averaging, exactly as in the submitted model.
            values += weight * np.maximum(
                estimator.predict(target[FEATURES], thread_count=history.attrs.get("model_threads", 2)), 0
            )
        values[target.route.isin(model["zero_routes"])] = 0
        return target[["route", "timestamp"]].assign(value=values)
