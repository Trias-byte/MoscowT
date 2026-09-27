import json
import os
import uuid
from datetime import datetime
from typing import Literal

import anyio
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse

from .data.catalog import RouteDefinition
from .data.schemas import ImportSpec
from .domain import DomainError, Scope, StrictModel, TimeRange
from .fleet import VEHICLE_LOAD_THRESHOLDS
from .modeling.adapters import ADAPTERS
from .modeling.contracts import ForecastSpec, TrainingSpec
from .modeling.packages import EnsembleWeights, ModelPackageService
from .modeling.service import ModelService
from .platform_exports import PeriodExporter, PeriodExportSpec
from .scenarios import ScenarioService, ScenarioSpec
from .schedules import ScheduleService
from .storage import digest
from .tasks import WorkQueue


class UploadRequest(StrictModel):
    blob_id: str
    spec: ImportSpec


class ScheduleRequest(StrictModel):
    blob_id: str


class CompetitionRequest(StrictModel):
    forecast_id: str


class PublishRequest(StrictModel):
    forecast_id: str
    expected_snapshot_id: str | None = None
    schedule_id: str | None = None
    schedule_scenario: bool = False


class MapViewRequest(StrictModel):
    forecast_id: str
    snapshot_id: str


class RouteRequest(StrictModel):
    route: RouteDefinition
    expected_version: str | None = None


class ModelImportRequest(StrictModel):
    blob_id: str


class FactorFetchRequest(StrictModel):
    kind: Literal["weather", "accidents"]
    start: str = "2024-01-01"
    end: str = "2025-12-31"
    network_id: str | None = None


def router(settings, data, store, view_cache):
    api = APIRouter(prefix="/api/v2")
    queue = WorkQueue(store.root, settings.queue_limit)
    models = ModelService(data, store, settings.model_threads, settings.max_forecast_hours)
    schedules = ScheduleService(settings.data_root)
    exporter = PeriodExporter(settings, data, models)

    from .spatial import SpatialService

    spatial = SpatialService(store, settings.data_root)

    @api.post("/sections")
    def sections(body: Scope):
        snapshot = store.read("snapshots", body.snapshotId)
        if body.scenarioId and store.read("scenarios", body.scenarioId)["spec"].get("engine") == "recompute":
            snapshot = {**snapshot, "scheduleId": None}
        if body.grain != "hour":
            raise DomainError("HOURLY_SPATIAL_REQUIRED", "Для расчёта участков нужен часовой шаг")
        view = view_cache.get(body)
        return {"meta": view["meta"], "sections": spatial.sections(snapshot, view["frames"])}

    @api.get("/capabilities")
    def capabilities():
        return {
            "contract_version": 2,
            "current_dataset_id": data.catalog.current_id(),
            "current_snapshot": store.current(),
            "timezone": "Europe/Moscow",
            "vehicle_load_thresholds": VEHICLE_LOAD_THRESHOLDS,
            "max_forecast_hours": settings.max_forecast_hours,
        }

    @api.post("/forecast-runs/{ident}/weather-profile")
    def weather_profile(ident: str, body: TimeRange):
        import pandas as pd

        from .external import OpenMeteoForecastProvider
        from .factors import hourly_weather

        run = store.read("forecast_runs", ident)
        spec = ForecastSpec.model_validate(run["spec"])
        model = store.read("trained_models", spec.model_id)
        source_id = model["spec"].get("weather_hourly_id")
        if not source_id or "weather" not in model["spec"].get("feature_groups", []):
            raise DomainError("WEATHER_UNSUPPORTED", "У модели нет почасовых погодных признаков")
        if body.start < spec.time_range.start or body.end > spec.time_range.end:
            raise DomainError("INVALID_SCENARIO_RANGE", "Период должен быть внутри выпуска")
        target = pd.DataFrame({"timestamp": pd.date_range(body.start, body.end, freq="h", inclusive="left")})
        profile = hourly_weather(
            str(settings.data_root), source_id, target, spec.origin, spec.diagnostic_observed_factors
        )
        method = "retrospective_actual" if spec.diagnostic_observed_factors else "prior_year_climatology"
        if spec.weather_forecast_id:
            profile = OpenMeteoForecastProvider(settings.data_root).select_hourly(
                spec.weather_forecast_id, target.timestamp, spec.origin
            )
            method = "available_forecast_release"
        return {
            "forecast_id": ident,
            "source_id": spec.weather_forecast_id or source_id,
            "method": method,
            "fields": {
                key: {
                    "min": float(profile[key].min()),
                    "max": float(profile[key].max()),
                    "mean": float(profile[key].mean()),
                }
                for key in profile.columns
            },
        }

    @api.post("/blobs", status_code=201)
    async def upload_blob(request: Request, kind: Literal["data", "model"] = "data"):
        ident = "blob-" + uuid.uuid4().hex
        path = store.path("uploads", ident, "zip" if kind == "model" else "csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            async with await anyio.open_file(path, "wb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > (1 if kind == "model" else 12) * 1024**3:
                        raise DomainError(
                            "UPLOAD_TOO_LARGE", "Превышен размер загрузки (модель 1 ГиБ; данные 12 ГиБ)", 413
                        )
                    await stream.write(chunk)
                await stream.flush()
                await anyio.to_thread.run_sync(os.fsync, stream.wrapped.fileno())
            if not size:
                raise DomainError("EMPTY_UPLOAD", "Файл пуст")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"id": ident, "bytes": size}

    @api.post("/uploads", status_code=202)
    def preview(body: UploadRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not store.path("uploads", body.blob_id, "csv").is_file():
            raise DomainError("UPLOAD_NOT_FOUND", "Загруженный файл не найден", 404)
        return queue.enqueue("import_preview", body.model_dump(mode="json"), idempotency_key)

    @api.get("/uploads/{ident}")
    def get_upload(ident: str):
        return data.get_preview(ident)

    @api.post("/uploads/{ident}/apply", status_code=202)
    def apply(ident: str, idempotency_key: str = Header(alias="Idempotency-Key")):
        data.get_preview(ident)
        return queue.enqueue("import_apply", {"upload_id": ident}, idempotency_key)

    @api.get("/datasets")
    def datasets():
        return {"current_id": data.catalog.current_id(), "versions": data.catalog.datasets()}

    @api.get("/datasets/{ident}")
    def dataset(ident: str):
        manifest = data.manifest(ident)
        return {
            **manifest,
            "dependent_models": [m["id"] for m in models.list_models() if m["spec"]["dataset_id"] == ident],
            "recalculation_policy": "explicit",
            "is_default": ident == data.catalog.current_id(),
        }

    @api.post("/datasets/{ident}/activate")
    def activate(ident: str):
        data.verify(ident)
        with data.catalog.connect() as db:
            db.execute(
                "INSERT INTO catalog_state(key,value) VALUES ('dataset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ident,),
            )
        return {"current_id": ident}

    @api.get("/routes")
    def routes():
        current = data.catalog.current_id()
        available = set(data.manifest(current)["routes"]) if current else set()
        trained = {
            route
            for model in models.list_models()
            for route in model["spec"]["route_ids"]
            if model["spec"]["dataset_id"] == current
        }
        return [
            {
                **route,
                "status": "model_ready"
                if route["id"] in trained
                else "history_available"
                if route["id"] in available
                else "registered",
            }
            for route in data.catalog.routes()
        ]

    @api.get("/routes/{ident}")
    def route(ident: str):
        result = next((r for r in routes() if r["id"] == ident), None)
        if result is None:
            raise DomainError("ROUTE_NOT_FOUND", "Маршрут не найден", 404)
        return result

    @api.post("/routes")
    def create_route(body: RouteRequest):
        return data.catalog.save_route(body.route, body.expected_version)

    @api.get("/external-sources")
    def external_sources():
        from .external import SOURCES, ExternalRepository

        reports = [
            json.loads(path.read_bytes()) for path in sorted((store.root / "evaluations").glob("*.json"))
        ]
        reports.sort(key=lambda report: report.get("created_at", ""))
        return {
            "sources": [
                {
                    **source,
                    "effect_status": reports[-1]
                    .get("effects", {})
                    .get(source["id"], {})
                    .get("status", source["effect_status"]),
                }
                for source in SOURCES
            ]
            if reports
            else SOURCES,
            "snapshots": ExternalRepository(settings.data_root).list(),
            "evaluations": reports,
        }

    @api.get("/weather-forecasts")
    def weather_forecasts():
        from .external import OpenMeteoForecastProvider

        return OpenMeteoForecastProvider(settings.data_root).list()

    @api.post("/weather-forecasts", status_code=202)
    def fetch_weather(idempotency_key: str = Header(alias="Idempotency-Key")):
        return queue.enqueue("weather_fetch", {}, idempotency_key)

    @api.post("/scenarios")
    def create_scenario(body: ScenarioSpec):
        return ScenarioService(store).create(body)

    @api.get("/scenarios/{ident}")
    def get_scenario(ident: str):
        record = store.read("scenarios", ident)
        if record.get("job_id") and queue.get(record["job_id"])["status"] != "ready":
            raise DomainError("SCENARIO_NOT_READY", "Расчёт сценария не завершён", 409)
        return record

    @api.post("/scenarios/{ident}/runs", status_code=202)
    def scenario_run(ident: str, idempotency_key: str = Header(alias="Idempotency-Key")):
        scenario = store.read("scenarios", ident)
        if scenario["spec"].get("engine") != "recompute":
            raise DomainError("SCENARIO_ENGINE_REQUIRED", "Для расчёта нужен сценарий нового формата")
        return queue.enqueue("scenario_run", {"scenario_id": ident}, idempotency_key)

    @api.get("/factor-datasets")
    def factors():
        from .factors import FactorRepository

        repo = FactorRepository(settings.data_root)
        return {kind: repo.list(kind) for kind in ("weather_hourly", "accidents", "accident_links")}

    @api.post("/factor-datasets", status_code=202)
    def fetch_factors(body: FactorFetchRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        from datetime import date

        try:
            start, end = date.fromisoformat(body.start), date.fromisoformat(body.end)
            if start > end or (end - start).days > 3660:
                raise ValueError()
        except ValueError:
            raise DomainError("INVALID_FACTOR_PERIOD", "Укажите даты YYYY-MM-DD, не более 10 лет") from None
        if body.kind == "accidents":
            body.network_id = body.network_id or store.current().get("networkId")
            if not body.network_id:
                raise DomainError("NETWORK_REQUIRED", "Для ДТП нужна датированная геометрия")
            store.read("networks", body.network_id)
        return queue.enqueue("factor_fetch", body.model_dump(), idempotency_key)

    @api.get("/accidents")
    def accidents(
        dataset_id: str,
        start: datetime,
        end: datetime,
        west: float = 36.5,
        south: float = 54.9,
        east: float = 38.3,
        north: float = 56.2,
    ):
        import pandas as pd

        from .factors import factor_frame

        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise DomainError("INVALID_ACCIDENT_PERIOD", "Нужен интервал с часовым поясом")
        manifest, frame = factor_frame(str(settings.data_root), "accidents", dataset_id)
        selected = frame.loc[
            frame.timestamp.ge(start)
            & frame.timestamp.lt(end)
            & frame.longitude.between(west, east)
            & frame.latitude.between(south, north)
        ]
        return {
            "dataset_id": dataset_id,
            "coverage": manifest["coverage"],
            "records": selected.astype(object).where(pd.notna(selected), None).to_dict("records"),
        }

    @api.get("/accident-candidates")
    def accident_candidates(longitude: float, latitude: float, date: str, snapshot_id: str):
        from datetime import date as civil_date

        from .factors import nearby_routes
        from .network_history import resolve_network

        try:
            civil_date.fromisoformat(date)
        except ValueError:
            raise DomainError("INVALID_DATE", "Ожидается дата YYYY-MM-DD") from None
        snapshot = store.read("snapshots", snapshot_id)
        network = resolve_network(store, store.read("networks", snapshot["networkId"]), date)
        return {
            "radius_m": 100,
            "routes": nearby_routes(network, longitude, latitude),
            "method": "proximity_candidate_not_confirmed_disruption",
        }

    @api.post("/models/import-preview")
    def model_preview(body: ModelImportRequest):
        if not store.path("uploads", body.blob_id, "zip").is_file():
            raise DomainError("UPLOAD_NOT_FOUND", "Пакет не найден", 404)
        meta = ModelPackageService(models).inspect(store.path("uploads", body.blob_id, "zip"))
        return {"format": meta["format"], "manifest": meta["manifest"], "estimators": meta["estimators"]}

    @api.post("/models/import", status_code=202)
    def model_import(body: ModelImportRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not store.path("uploads", body.blob_id, "zip").is_file():
            raise DomainError("UPLOAD_NOT_FOUND", "Пакет не найден", 404)
        return queue.enqueue("model_import", body.model_dump(), idempotency_key)

    @api.post("/models/{ident}/weights", status_code=202)
    def model_weights(
        ident: str,
        body: EnsembleWeights,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        forecast_id: str | None = None,
    ):
        store.read("trained_models", ident)
        if forecast_id and store.read("forecast_runs", forecast_id)["spec"]["model_id"] != ident:
            raise DomainError("MODEL_FORECAST_MISMATCH", "Выбранный выпуск создан другой моделью")
        return queue.enqueue(
            "model_reweight",
            {"model_id": ident, "weights": body.model_dump(), "forecast_id": forecast_id},
            idempotency_key,
        )

    @api.post("/models/{ident}/export", status_code=202)
    def model_export(ident: str, idempotency_key: str = Header(alias="Idempotency-Key")):
        store.read("trained_models", ident)
        return queue.enqueue("model_export", {"model_id": ident}, idempotency_key)

    @api.get("/model-types")
    def model_types():
        return [adapter.capabilities() for adapter in ADAPTERS.values()]

    @api.get("/models")
    def list_models():
        return [m for m in models.list_models() if m["spec"].get("purpose", "service") == "service"]

    @api.get("/model-evaluations")
    def model_evaluations():
        return [
            json.loads(path.read_bytes())
            for path in sorted((store.root / "reports").glob("validation-*.json"))
        ]

    @api.post("/training-runs", status_code=202)
    def train(body: TrainingSpec, idempotency_key: str = Header(alias="Idempotency-Key")):
        data.manifest(body.dataset_id)
        return queue.enqueue("train", body.model_dump(mode="json"), idempotency_key)

    @api.post("/forecast-runs", status_code=202)
    def forecast(body: ForecastSpec, idempotency_key: str = Header(alias="Idempotency-Key")):
        store.read("trained_models", body.model_id)
        data.manifest(body.dataset_id)
        return queue.enqueue("forecast", body.model_dump(mode="json"), idempotency_key)

    @api.get("/forecast-runs")
    def forecast_runs():
        return [
            f
            for f in models.list_forecasts()
            if f.get("purpose", "service") == "service" and not f["spec"].get("diagnostic_observed_factors")
        ]

    @api.get("/forecast-runs/{ident}")
    def forecast_run(ident: str):
        return store.read("forecast_runs", ident)

    @api.get("/forecast-runs/{ident}/evaluation")
    def evaluate(ident: str, truth_dataset_id: str):
        return models.evaluate(ident, truth_dataset_id)

    @api.post("/forecasts/query")
    def query(body: PeriodExportSpec):
        frame = exporter.frame(body)
        return {
            "spec": body.model_dump(mode="json"),
            "rows": frame.astype(object).where(frame.notna(), None).to_dict(orient="records"),
        }

    @api.get("/forecasts")
    def forecasts(forecast_id: str, start: datetime, end: datetime, route_ids: list[str] = Query()):
        manifest = store.read("forecast_runs", forecast_id)
        return query(
            PeriodExportSpec(
                dataset_id=manifest["spec"]["dataset_id"],
                forecast_id=forecast_id,
                route_ids=route_ids,
                time_range=TimeRange(start=start, end=end),
                mode="forecast",
            )
        )

    @api.post("/publications")
    def publish(body: PublishRequest):
        from .publication import publish_forecast

        return publish_forecast(settings, data, models, body)

    @api.post("/forecast-map-views")
    def forecast_map_view(body: MapViewRequest):
        from .publication import publish_forecast

        base = store.read("snapshots", body.snapshot_id)
        run = store.read("forecast_runs", body.forecast_id)
        if run["spec"]["dataset_id"] != base.get("datasetId"):
            raise DomainError("MODEL_DATASET_MISMATCH", "Для другого набора сначала опубликуйте его выпуск")
        request = PublishRequest(
            forecast_id=body.forecast_id,
            schedule_id=base.get("scheduleId"),
            schedule_scenario=base.get("scheduleScenario", False),
        )
        return publish_forecast(settings, data, models, request, base_snapshot=base, activate=False)

    @api.get("/schedules")
    def list_schedules():
        return schedules.list()

    @api.post("/schedules", status_code=202)
    def import_schedule(body: ScheduleRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not store.path("uploads", body.blob_id, "csv").is_file():
            raise DomainError("UPLOAD_NOT_FOUND", "Загруженный файл не найден", 404)
        return queue.enqueue("schedule_import", body.model_dump(), idempotency_key)

    @api.get("/schedules/{ident}/reconciliation")
    def reconcile(ident: str, dataset_id: str):
        manifest = schedules.store.read("schedules", ident)
        return schedules.reconcile(
            ident,
            data.event_batches(
                dataset_id,
                start=manifest["valid_from"] + "T00:00:00+03:00",
                end=manifest["valid_to"] + "T00:00:00+03:00",
            ),
        )

    @api.post("/exports", status_code=202)
    def export(body: PeriodExportSpec, idempotency_key: str = Header(alias="Idempotency-Key")):
        return queue.enqueue("period_export", body.model_dump(mode="json"), idempotency_key)

    @api.post("/competition-exports", status_code=202)
    def competition_export(body: CompetitionRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        from .domain import FINAL_END, HISTORY_END, ROUTES

        run = store.read("forecast_runs", body.forecast_id)
        spec = PeriodExportSpec(
            dataset_id=run["spec"]["dataset_id"],
            forecast_id=body.forecast_id,
            route_ids=[str(route) for route in ROUTES],
            time_range=TimeRange(start=HISTORY_END, end=FINAL_END),
            mode="forecast",
            format="competition",
        )
        return queue.enqueue("period_export", spec.model_dump(mode="json"), idempotency_key)

    @api.post("/bundles", status_code=202)
    def bundle(idempotency_key: str = Header(alias="Idempotency-Key")):
        return queue.enqueue(
            "bundle_export",
            {
                "snapshot": store.current(),
                "dataset_id": data.catalog.current_id(),
                "versions": digest(models.list_forecasts()),
            },
            idempotency_key,
        )

    @api.get("/jobs")
    def jobs():
        return queue.list()

    @api.get("/jobs/{ident}")
    def job(ident: str):
        return queue.public(queue.get(ident))

    @api.post("/jobs/{ident}/cancel")
    def cancel(ident: str):
        return queue.cancel(ident)

    @api.get("/jobs/{ident}/download")
    def download(ident: str):
        job = queue.public(queue.get(ident))
        if job["status"] != "ready" or not job["result"].get("filename"):
            raise DomainError("EXPORT_NOT_READY", "Файл ещё не готов", 409)
        filename = job["result"]["filename"]
        download_name = "submission.csv" if job["result"].get("spec", {}).get("format") in ("submission", "competition") else filename
        return FileResponse(store.root / "exports" / filename, filename=download_name)

    return api
