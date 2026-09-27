import importlib.metadata
import io
import json
import resource
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ..data.repository import DatasetRepository
from ..domain import TZ, DomainError
from ..storage import SnapshotStore, atomic_write, digest, file_hash
from .adapters import ADAPTERS
from .contracts import ForecastSpec, TrainingSpec


class ModelService:
    def __init__(self, data: DatasetRepository, store: SnapshotStore, threads=2, max_hours=366 * 24):
        self.data, self.store, self.threads, self.max_hours = data, store, threads, max_hours

    def list_models(self):
        return self._list("trained_models")

    def list_forecasts(self):
        return self._list("forecast_runs")

    def _list(self, kind):
        return [json.loads(path.read_bytes()) for path in sorted((self.store.root / kind).glob("*.json"))]

    def _history(self, dataset_id, routes, start, end, minimum_days):
        manifest = self.data.manifest(dataset_id)
        if set(routes) - set(manifest["routes"]):
            raise DomainError(
                "ROUTE_DATA_UNAVAILABLE", "У выбранных маршрутов нет истории в этой версии данных"
            )
        history = self.data.frame(dataset_id, route_ids=routes, start=start, end=end)
        for route in routes:
            known = history.loc[(history.route == route) & history.value.notna()]
            if known.empty or (known.timestamp.max() - known.timestamp.min()) < pd.Timedelta(
                days=minimum_days, hours=-1
            ):
                raise DomainError(
                    "INSUFFICIENT_HISTORY", f"Маршруту {route} нужно не менее {minimum_days} дней истории"
                )
            coverage = known.groupby([known.timestamp.dt.dayofweek, known.timestamp.dt.hour]).size()
            if len(coverage) != 168 or coverage.min() < 3:
                raise DomainError(
                    "INSUFFICIENT_HISTORY",
                    f"Маршрут {route}: нужны хотя бы три наблюдения каждого дня недели и часа",
                )
        history.attrs["external_root"] = str(self.data.root)
        return history

    def train(self, spec: TrainingSpec):
        start = time.monotonic()
        if set(spec.feature_groups) & {"weather", "events"} and spec.model_type != "lgb_cb_rf":
            raise DomainError(
                "FEATURES_UNSUPPORTED",
                "Внешние ML-признаки поддерживает ансамбль; для остальных моделей используйте сценарные поправки",
            )
        if set(spec.feature_groups) & {"weather", "events"} and not spec.external_snapshot_id:
            raise DomainError("EXTERNAL_DATA_REQUIRED", "Выберите версию внешних данных")
        adapter = ADAPTERS[spec.model_type]
        history = self._history(
            spec.dataset_id, spec.route_ids, spec.time_range.start, spec.time_range.end, adapter.minimum_days
        )
        if spec.external_snapshot_id:
            from ..external import ExternalRepository

            ExternalRepository(self.data.root).frame(spec.external_snapshot_id)
        environment = {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pandas", "catboost", "lightgbm", "scikit-learn", "holidays", "joblib")
        }
        code = {path.name: file_hash(path) for path in Path(__file__).parent.glob("*.py")}
        code.update(
            {
                name: file_hash(Path(__file__).parent.parent / name)
                for name in ("external.py", "service_calendar.py")
            }
        )
        identity = {
            "spec": spec.model_dump(mode="json"),
            "recipe": adapter.version,
            "environment": environment,
            "code": code,
            "threads": self.threads,
            "capabilities": adapter.capabilities(),
        }
        ident = "model-" + digest(identity)
        with self.store.lock("train"):
            if self.store.path("trained_models", ident).exists():
                return self.store.read("trained_models", ident)
            model = adapter.fit(history, spec, self.threads)
            self.save(ident, model)
            manifest = {
                "id": ident,
                **identity,
                "artifact_sha256": file_hash(self.store.path("trained_models", ident, "joblib")),
                "created_at": datetime.now(TZ).isoformat(),
                "duration_seconds": round(time.monotonic() - start, 3),
                "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                "availability_policy": "retrospective_event_time",
                "training_rows": int(history.value.notna().sum()),
                "route_coverage": {
                    route: {
                        "known_hours": int(rows.value.notna().sum()),
                        "positive_hours": int(rows.value.gt(0).sum()),
                        "missing_hours": int(rows.value.isna().sum()),
                        "quality_note": "no_positive_history; transfer unvalidated"
                        if not rows.value.gt(0).any()
                        else "see_route_evaluation",
                    }
                    for route, rows in history.groupby("route")
                },
            }
            self.store.put("trained_models", ident, manifest)
        return manifest

    def save(self, ident, model):
        import joblib

        output = io.BytesIO()
        joblib.dump(model, output, compress=3)
        atomic_write(self.store.path("trained_models", ident, "joblib"), output.getvalue())

    def load(self, ident):
        import joblib

        manifest = self.store.read("trained_models", ident)
        path = self.store.path("trained_models", ident, "joblib")
        if not path.is_file() or file_hash(path) != manifest["artifact_sha256"]:
            raise DomainError("MODEL_CORRUPT", "Контрольная сумма модели не совпадает", 503)
        # Only artifacts trained locally may enter this store; arbitrary pickle uploads are not accepted.
        return manifest, joblib.load(path)

    def predict(self, spec: ForecastSpec):
        manifest, model = self.load(spec.model_id)
        training = TrainingSpec.model_validate(manifest["spec"])
        if training.time_range.end > spec.origin:
            raise DomainError("FUTURE_TRAINING_DATA", "Обучение использует данные после момента выпуска")
        if set(spec.route_ids) - set(training.route_ids):
            raise DomainError("ROUTE_NOT_TRAINED", "Для новых маршрутов требуется обучение модели")
        if (spec.time_range.end - spec.origin).total_seconds() > self.max_hours * 3600:
            raise DomainError("HORIZON_UNSUPPORTED", f"Максимум {self.max_hours} часов от origin")
        if spec.dataset_id != training.dataset_id:
            raise DomainError(
                "MODEL_DATASET_MISMATCH",
                "После изменения данных переобучите модель; старый выпуск доступен по его версии",
                409,
            )
        if (
            training.model_type != "annual_scenario"
            and (spec.time_range.end - spec.origin).total_seconds() > 62 * 24 * 3600
        ):
            raise DomainError(
                "HORIZON_UNSUPPORTED", "Для горизонта более 62 суток выберите годовой сценарный адаптер"
            )
        adapter = ADAPTERS[training.model_type]
        history = self._history(
            spec.dataset_id, spec.route_ids, training.time_range.start, spec.origin, adapter.minimum_days
        )
        if spec.weather_forecast_id:
            if "weather" not in training.feature_groups or training.model_type != "lgb_cb_rf":
                raise DomainError(
                    "WEATHER_MODEL_REQUIRED", "Выбранная модель не обучена с погодными признаками"
                )
            if (spec.time_range.end - spec.origin).total_seconds() > 24 * 3600:
                raise DomainError(
                    "WEATHER_HORIZON_UNSUPPORTED",
                    "Оперативный погодный выпуск применяется только к горизонту до суток",
                )
            history.attrs["weather_forecast_id"] = spec.weather_forecast_id
        inference_code = {path.name: file_hash(path) for path in Path(__file__).parent.glob("*.py")}
        inference_code.update(
            {
                name: file_hash(Path(__file__).parent.parent / name)
                for name in ("external.py", "service_calendar.py")
            }
        )
        ident = "forecast-" + digest(
            {
                "spec": spec.model_dump(mode="json"),
                "model_sha256": manifest["artifact_sha256"],
                "inference_code": inference_code,
            }
        )
        with self.store.lock("forecast"):
            if self.store.path("forecast_runs", ident).exists():
                return self.store.read("forecast_runs", ident)
            predicted = adapter.predict(model, history, spec)
            expected = len(spec.route_ids) * int(
                (spec.time_range.end - spec.time_range.start).total_seconds() / 3600
            )
            if (
                len(predicted) != expected
                or predicted.duplicated(["route", "timestamp"]).any()
                or not np.isfinite(predicted.value).all()
                or (predicted.value < 0).any()
            ):
                raise DomainError(
                    "INVALID_PREDICTIONS", "Модель вернула неполный или некорректный прогноз", 500
                )
            buffer = io.BytesIO()
            predicted.to_parquet(buffer, index=False)
            path = self.store.path("forecast_runs", ident, "parquet")
            atomic_write(path, buffer.getvalue())
            result = {
                "id": ident,
                "spec": spec.model_dump(mode="json"),
                "recipe": adapter.version,
                "inference_code": inference_code,
                "created_at": datetime.now(TZ).isoformat(),
                "rows": len(predicted),
                "total": float(predicted.value.sum()),
                "sha256": file_hash(path),
                "availability_policy": "retrospective_event_time",
                "quality_note": "Невалидированный годовой сценарий"
                if training.model_type == "annual_scenario"
                else "Качество см. в отчёте временной проверки",
                "model_type": training.model_type,
                "external_snapshot_id": training.external_snapshot_id,
                "route_applicability": manifest.get("route_coverage", {}),
                "weather_method": "received_forecast_unvalidated_transfer"
                if spec.weather_forecast_id
                else "prior_year_climatology"
                if "weather" in training.feature_groups
                else "not_used",
            }
            self.store.put("forecast_runs", ident, result)
        return result

    def frame(self, ident, start=None, end=None, routes=None):
        manifest = self.store.read("forecast_runs", ident)
        frame = pd.read_parquet(self.store.path("forecast_runs", ident, "parquet"))
        if start is not None:
            frame = frame.loc[frame.timestamp >= pd.Timestamp(start)]
        if end is not None:
            frame = frame.loc[frame.timestamp < pd.Timestamp(end)]
        if routes:
            frame = frame.loc[frame.route.isin(routes)]
        return manifest, frame.reset_index(drop=True)

    def evaluate(self, ident, truth_id):
        manifest, forecast = self.frame(ident)
        spec = ForecastSpec.model_validate(manifest["spec"])
        truth = self.data.frame(
            truth_id, route_ids=spec.route_ids, start=spec.time_range.start, end=spec.time_range.end
        )
        merged = forecast.merge(
            truth[["route", "timestamp", "value"]],
            on=["route", "timestamp"],
            suffixes=("_prediction", "_truth"),
            validate="one_to_one",
        )
        known = merged.dropna(subset=["value_truth"])

        def score(rows):
            denominator = rows.value_truth.abs().sum()
            return (
                float((rows.value_truth - rows.value_prediction).abs().sum() / denominator)
                if denominator
                else None
            )

        return {
            "forecast_id": ident,
            "truth_dataset_id": truth_id,
            "matched_rows": len(known),
            "expected_rows": len(forecast),
            "wape": score(known),
            "route_wape": {route: score(rows) for route, rows in known.groupby("route")},
            "availability_policy": "retrospective_event_time",
        }
