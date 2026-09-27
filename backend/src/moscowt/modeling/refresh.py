"""Durable import → validation → refit → forecast → guarded publication."""

import json
import time
from urllib.error import URLError

import pandas as pd

from ..domain import DomainError, TimeRange
from ..storage import canonical, digest
from ..tasks import WorkQueue
from .contracts import ForecastSpec, TrainingSpec
from .coverage import summarize_history
from .packages import EnsembleWeights, ModelPackageService


def is_descendant(data, ident, ancestor):
    seen = set()
    while ident and ident not in seen:
        if ident == ancestor:
            return True
        seen.add(ident)
        ident = data.manifest(ident).get("parent_id")
    return False


def update_payload(data, store, *, upload_id=None, dataset_id=None, model_id=None):
    current = store.current()
    forecast = store.read("forecast_runs", current["forecastId"])
    source_model = model_id or forecast["spec"]["model_id"]
    model = store.read("trained_models", source_model)
    if model["spec"].get("purpose", "service") != "service" or model["spec"]["model_type"] not in (
        "lgb_cb_rf",
        "competition_catboost",
    ):
        raise DomainError(
            "REFRESH_MODEL_REQUIRED",
            "Для автоматического обновления выберите базу CatBoost или сервисный ансамбль",
            409,
        )
    return {
        "upload_id": upload_id,
        "dataset_id": dataset_id,
        "source_model_id": source_model,
        "base_dataset_id": model["spec"]["dataset_id"],
        "expected_snapshot_id": current["snapshotId"],
        "schedule_id": current.get("scheduleId"),
        "schedule_scenario": current.get("scheduleScenario", False),
    }


def score(truth, prediction):
    joined = (
        truth[["route", "timestamp", "value"]]
        .rename(columns={"value": "truth"})
        .merge(
            prediction.rename(columns={"value": "prediction"}),
            on=["route", "timestamp"],
            validate="one_to_one",
        )
        .dropna(subset=["truth", "prediction"])
    )
    return {
        "absolute_error": float((joined.prediction - joined.truth).abs().sum()),
        "absolute_truth": float(joined.truth.abs().sum()),
        "rows": len(joined),
    }


def aggregate_score(scores):
    rows = sum(s["rows"] for s in scores)
    truth = sum(s["absolute_truth"] for s in scores)
    error = sum(s["absolute_error"] for s in scores)
    return {
        "metric": "wape" if truth else "mae",
        "value": error / (truth or rows) if rows else None,
        "rows": rows,
    }


def quality_gate(baseline, candidate):
    if not baseline["rows"] or not candidate["rows"]:
        return {"passed": False, "reason": "INSUFFICIENT_VALIDATION", "threshold": 0.05}
    limit = baseline["value"] * 1.05
    return {
        "passed": candidate["value"] <= limit + 1e-9,
        "reason": "ACCEPTED" if candidate["value"] <= limit + 1e-9 else "QUALITY_REGRESSION",
        "threshold": 0.05,
    }


class ModelRefresh:
    def __init__(self, settings, data, models, job):
        self.settings, self.data, self.models, self.job = settings, data, models, job
        self.store = models.store
        self.queue = WorkQueue(settings.state_dir, settings.queue_limit)
        self.payload = job["payload"]
        self.state = json.loads(self.queue.get(job["id"])["checkpoint"])
        self.source, fitted = models.load(self.payload["source_model_id"])
        self.original = TrainingSpec.model_validate(self.source["spec"])
        self.competition = self.original.model_type == "competition_catboost"
        names = {
            "CatBoostRegressor": "catboost",
            "LGBMRegressor": "lightgbm",
            "Booster": "lightgbm",
            "RandomForestRegressor": "random_forest",
        }
        self.weights = (
            None
            if self.competition
            else EnsembleWeights.model_validate(
                {names[type(estimator).__name__]: weight for weight, estimator in fitted["models"]}
            )
        )

    def stage(self, phase, progress, **changes):
        self.state = self.queue.checkpoint(self.job["id"], self.job["owner"], phase, progress, **changes)

    def fit(self, spec):
        model = self.models.train(spec)
        if self.weights is not None and self.weights != EnsembleWeights():
            model = ModelPackageService(self.models).reweight(model["id"], self.weights)
        return model

    def predict(self, model, origin, end, routes, *, persist=False):
        return self.models.predict(
            ForecastSpec(
                model_id=model["id"],
                dataset_id=model["spec"]["dataset_id"],
                route_ids=routes,
                origin=origin,
                time_range=TimeRange(start=origin, end=end),
            ),
            persist=persist,
        )

    def prepare(self, dataset_id):
        frame = self.data.frame(dataset_id, start=self.original.time_range.start)
        coverage = summarize_history(frame)
        if not coverage["forecast_origin"]:
            raise DomainError("INSUFFICIENT_HISTORY", "Нет фактических значений для обучения")
        origin = pd.Timestamp(coverage["forecast_origin"])
        if self.competition:
            origin = origin.normalize()
        routes, excluded = [], {}
        for route in sorted(frame.route.unique()):
            try:
                self.models._history(dataset_id, [route], self.original.time_range.start, origin, 56)
                if self.competition:
                    from .baseline_features import BaselineFeatures

                    BaselineFeatures(
                        frame.loc[frame.route == route], [route], self.original.time_range.start, origin
                    )
                routes.append(route)
            except DomainError as error:
                if (
                    error.code
                    not in ("INSUFFICIENT_HISTORY", "ROUTE_DATA_UNAVAILABLE", "INCOMPLETE_BASELINE_HISTORY")
                    or route in self.original.route_ids
                ):
                    raise
                excluded[route] = str(error)
        # A newly registered route without enough history cannot move the forecast origin.
        origin = pd.Timestamp(summarize_history(frame.loc[frame.route.isin(routes)])["forecast_origin"])
        if self.competition:
            origin = origin.normalize()
        training = self.original.model_copy(
            update={
                "dataset_id": dataset_id,
                "route_ids": routes,
                "time_range": TimeRange(start=self.original.time_range.start, end=origin),
            }
        )
        if "weather" in training.feature_groups and training.weather_hourly_id:
            from ..factors import FactorRepository, hourly_weather

            target = (
                frame.loc[frame.route.isin(routes) & frame.value.notna(), ["timestamp"]]
                .drop_duplicates()
                .reset_index(drop=True)
            )
            climate_target = pd.DataFrame(
                {
                    "timestamp": pd.date_range(
                        origin, origin + pd.DateOffset(years=1), freq="h", inclusive="left"
                    )
                }
            )

            def verify_weather(ident):
                hourly_weather(str(self.data.root), ident, target, origin, observed=True)
                # Both the annual release and the earliest validation cutoff need a full climate cycle.
                for cutoff in (origin, pd.Timestamp(training.time_range.start) + pd.Timedelta(days=56)):
                    hourly_weather(str(self.data.root), ident, climate_target, cutoff)

            try:
                verify_weather(training.weather_hourly_id)
            except DomainError as error:
                if error.code != "WEATHER_COVERAGE_MISSING":
                    raise
                try:
                    archive = FactorRepository(self.data.root).fetch_weather(
                        f"{training.time_range.start.year - 1}-01-01",
                        str((origin - pd.Timedelta(hours=1)).date()),
                    )
                except (OSError, URLError, ValueError) as error:
                    raise DomainError(
                        "WEATHER_REFRESH_FAILED",
                        "Не удалось получить недостающую погоду. Прежний прогноз сохранён; повторите обновление позже",
                        503,
                    ) from error
                training = training.model_copy(update={"weather_hourly_id": archive["id"]})
                verify_weather(archive["id"])
        return training, coverage, excluded

    def evaluation(self, training):
        routes = sorted(set(training.route_ids) & set(self.original.route_ids))
        end = pd.Timestamp(training.time_range.end)
        candidate_scores, baseline_scores, folds = [], [], []
        window = 61 if self.competition else 28
        for offset in (3 * window, 2 * window, window):
            origin = end - pd.Timedelta(days=offset)
            until = origin + pd.Timedelta(days=window)
            if origin <= training.time_range.start or origin <= self.original.time_range.start:
                continue
            truth = self.data.frame(training.dataset_id, route_ids=routes, start=origin, end=until)
            truth = truth.loc[truth.value.notna()]
            if set(truth.route) != set(routes):
                continue
            self.stage("validation", 0.25 + len(folds) * 0.06)
            try:
                candidate = self.fit(
                    training.model_copy(
                        update={
                            "purpose": "research",
                            "route_ids": routes,
                            "time_range": TimeRange(start=training.time_range.start, end=origin),
                        }
                    )
                )
                baseline = self.fit(
                    self.original.model_copy(
                        update={
                            "purpose": "research",
                            "route_ids": routes,
                            "time_range": TimeRange(
                                start=self.original.time_range.start,
                                end=min(origin, self.original.time_range.end),
                            ),
                        }
                    )
                )
                new = score(truth, self.predict(candidate, origin, until, routes))
                old = score(truth, self.predict(baseline, origin, until, routes))
            except DomainError as error:
                if error.code not in ("INSUFFICIENT_HISTORY", "ROUTE_DATA_UNAVAILABLE"):
                    raise
                continue
            candidate_scores.append(new)
            baseline_scores.append(old)
            folds.append(
                {
                    "origin": origin.isoformat(),
                    "end": until.isoformat(),
                    "candidate_model_id": candidate["id"],
                    "baseline_model_id": baseline["id"],
                    "candidate": aggregate_score([new]),
                    "baseline": aggregate_score([old]),
                }
            )
        baseline, candidate = aggregate_score(baseline_scores), aggregate_score(candidate_scores)
        gate = quality_gate(baseline, candidate)
        horizons = []
        # Separate long-horizon evidence uses the earliest cutoff with sufficient history for every route.
        for days in (1, 7, 30, 61) if self.competition else (1, 7, 30, 90, 180, 365):
            horizons.append({"days": days, "status": "insufficient_history"})
        if folds:
            frame = self.data.frame(
                training.dataset_id, route_ids=training.route_ids, start=training.time_range.start
            )
            known = frame.loc[frame.value.notna()]
            earliest = max(known.groupby("route").timestamp.min()) + pd.Timedelta(days=56)
            origin = earliest.ceil("h")
            try:
                model = self.fit(
                    training.model_copy(
                        update={
                            "purpose": "research",
                            "time_range": TimeRange(start=training.time_range.start, end=origin),
                        }
                    )
                )
                for entry in horizons:
                    until = origin + pd.Timedelta(days=entry["days"])
                    if until > end:
                        continue
                    self.stage("validation", 0.45)
                    truth = self.data.frame(
                        training.dataset_id, route_ids=training.route_ids, start=origin, end=until
                    )
                    measured = aggregate_score(
                        [score(truth, self.predict(model, origin, until, training.route_ids))]
                    )
                    entry.update(
                        status="evaluated" if measured["rows"] else "insufficient_history",
                        origin=origin.isoformat(),
                        **measured,
                    )
            except DomainError as error:
                if error.code != "INSUFFICIENT_HISTORY":
                    raise
        report = {
            "dataset_id": training.dataset_id,
            "source_model_id": self.source["id"],
            "baseline": baseline,
            "candidate": candidate,
            "gate": gate,
            "folds": folds,
            "horizons": horizons,
            "policy": f"fixed_origin_{window}d_three_windows; targets only after cutoff; no independent competition/annual accuracy claim",
        }
        report["id"] = "validation-" + digest(report)
        self.store.put("reports", report["id"], report)
        return report

    def run(self):
        try:
            return self._run()
        except DomainError as error:
            if error.code != "JOB_INTERRUPTED":
                self.stage(
                    self.queue.get(self.job["id"])["phase"],
                    self.queue.get(self.job["id"])["progress"],
                    error_code=error.code,
                )
            raise

    def _run(self):
        self.stage("import", 0.02, error_code=None)
        current = self.store.current()
        # A worker may have committed the filesystem pointer just before losing its process.
        if self.state.get("forecast_id") and current.get("forecastId") == self.state["forecast_id"]:
            return self.commit(current)
        if current.get("snapshotId") != self.payload["expected_snapshot_id"]:
            raise DomainError(
                "SNAPSHOT_CHANGED",
                "Публикация изменилась; запустите обновление выбранной редакции заново",
                409,
            )
        if not self.state.get("dataset_id"):
            applied = (
                self.data.apply(self.payload["upload_id"])
                if self.payload.get("upload_id")
                else {"dataset_id": self.payload["dataset_id"]}
            )
            self.stage("factors", 0.1, dataset_id=applied["dataset_id"])
        dataset_id = self.state["dataset_id"]
        before = self.data.frame(self.original.dataset_id)[["route", "timestamp", "value"]].dropna(
            subset=["value"]
        )
        after = self.data.frame(dataset_id)[["route", "timestamp", "value"]].dropna(subset=["value"])
        if before.reset_index(drop=True).equals(after.reset_index(drop=True)):
            self.stage("unchanged", 1, publication_status="unchanged", model_id=self.source["id"])
            return {**self.public_result(), "duplicate": True}
        if not self.state.get("training"):
            self.stage("factors", 0.1)
            training, coverage, excluded = self.prepare(dataset_id)
            self.stage(
                "validation",
                0.2,
                training=training.model_dump(mode="json"),
                origin=training.time_range.end.isoformat(),
                coverage=coverage,
                excluded_routes=excluded,
            )
        training = TrainingSpec.model_validate(self.state["training"])
        if not self.state.get("evaluation_id"):
            report = self.evaluation(training)
            self.stage("validation", 0.5, evaluation_id=report["id"])
        report = self.store.read("reports", self.state["evaluation_id"])
        if not report["gate"]["passed"]:
            self.stage("quality_blocked", 0.5, publication_status="quality_blocked")
            reason = report["gate"]["reason"]
            raise DomainError(
                reason,
                "Прежний прогноз сохранён: ошибка проверки выросла более чем на 5%"
                if reason == "QUALITY_REGRESSION"
                else "Прежний прогноз сохранён: недостаточно истории для проверки качества",
                409,
            )
        if not self.state.get("model_id"):
            self.stage("training", 0.55)
            model = self.fit(training)
            self.stage("forecast", 0.75, model_id=model["id"])
        model = self.store.read("trained_models", self.state["model_id"])
        if not self.state.get("forecast_id"):
            self.stage("forecast", 0.75)
            origin = pd.Timestamp(training.time_range.end)
            forecast = self.predict(
                model,
                origin,
                origin + (pd.Timedelta(days=61) if self.competition else pd.DateOffset(years=1)),
                training.route_ids,
                persist=True,
            )
            self.stage("publication", 0.95, forecast_id=forecast["id"])
        return self.commit()

    def public_result(self):
        return {
            key: self.state[key]
            for key in (
                "dataset_id",
                "model_id",
                "forecast_id",
                "evaluation_id",
                "origin",
                "excluded_routes",
                "publication_status",
            )
            if key in self.state
        }

    def commit(self, recovered=None):
        from ..platform_api import PublishRequest
        from ..publication import publish_forecast

        with self.queue.guarded(self.job["id"], self.job["owner"]) as db:
            snapshot = recovered or publish_forecast(
                self.settings,
                self.data,
                self.models,
                PublishRequest(
                    forecast_id=self.state["forecast_id"],
                    expected_snapshot_id=self.payload["expected_snapshot_id"],
                    schedule_id=self.payload["schedule_id"],
                    schedule_scenario=self.payload["schedule_scenario"],
                ),
            )
            result = {
                **self.public_result(),
                "snapshot_id": snapshot["snapshotId"],
                "publication_status": "published",
            }
            with self.data.catalog.connect() as catalog:
                catalog.execute(
                    "INSERT INTO catalog_state(key,value) VALUES ('dataset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (self.state["dataset_id"],),
                )
            db.execute(
                "UPDATE work_items SET status='ready',progress=1,phase='published',result=?,error=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (canonical(result).decode(), time.time(), self.job["id"]),
            )
        return result
