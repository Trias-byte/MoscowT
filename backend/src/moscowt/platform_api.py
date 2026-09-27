import json
import os
import uuid
from datetime import datetime

import anyio
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse

from .data.catalog import RouteDefinition
from .data.schemas import ImportSpec
from .domain import DomainError, Scope, StrictModel, TimeRange
from .fleet import VEHICLE_LOAD_THRESHOLDS
from .modeling.adapters import ADAPTERS
from .modeling.contracts import ForecastSpec, TrainingSpec
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


class RouteRequest(StrictModel):
    route: RouteDefinition
    expected_version: str | None = None


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

    @api.post("/blobs", status_code=201)
    async def upload_blob(request: Request):
        ident = "blob-" + uuid.uuid4().hex
        path = store.path("uploads", ident, "csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            async with await anyio.open_file(path, "wb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 12 * 1024**3:
                        raise DomainError("UPLOAD_TOO_LARGE", "Файл превышает 12 ГиБ", 413)
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
            "stale": ident != data.catalog.current_id(),
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
        return store.read("scenarios", ident)

    @api.get("/model-types")
    def model_types():
        return [adapter.capabilities() for adapter in ADAPTERS.values()]

    @api.get("/models")
    def list_models():
        return models.list_models()

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
        return models.list_forecasts()

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
        return FileResponse(store.root / "exports" / filename, filename=filename)

    return api
