import asyncio
import json
import logging
import multiprocessing as mp
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date as CalendarDate, datetime

import anyio
from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .analytics import NetworkCatalogService, RouteAnalyticsService
from .config import Settings
from .domain import METRIC, ROUTES, DomainError, ExportRequest, Scope, SubmissionRequest
from .exports import validate_submission_run
from .fleet import VEHICLE_LOAD_THRESHOLDS
from .jobs import JobRepository
from .network_history import resolve_network
from .route_imports import ApplyImportRequest, RouteImportRequest, apply_import, preview_import
from .storage import SnapshotStore, canonical
from .wire import (
    CapabilitiesResponse,
    ComparisonResponse,
    GeometrySelection,
    HeatmapResponse,
    JobResponse,
    NetworkResponse,
    Route,
    SeriesResponse,
    SnapshotResponse,
)
from .worker import run_worker


class ViewCache:
    def __init__(self, service, settings):
        self.service, self.settings = service, settings
        self.entries, self.size, self.lock = OrderedDict(), 0, threading.RLock()

    def get(self, scope):
        numeric = scope.model_dump(mode="json", exclude={"geometry"})
        key = canonical(numeric)
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key][0]
            value = self.service.view(scope)
            # Conservative allowance for the Python objects as well as encoded response.
            size = len(canonical(value)) * 8
            if size <= self.settings.cache_bytes:
                while self.entries and (
                    len(self.entries) >= self.settings.cache_entries
                    or self.size + size > self.settings.cache_bytes
                ):
                    _, (_, removed) = self.entries.popitem(last=False)
                    self.size -= removed
                self.entries[key] = (value, size)
                self.size += size
            return value


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = SnapshotStore(settings.state_dir)
    jobs = JobRepository(store.root, settings.queue_limit)
    from .data.repository import DatasetRepository

    data = DatasetRepository(settings.data_root)
    analytics = RouteAnalyticsService(store, data)
    cache = ViewCache(analytics, settings)
    network = NetworkCatalogService(store)
    registry = CollectorRegistry()
    duration = Histogram(
        "moscowt_http_seconds",
        "HTTP duration",
        ["method", "path"],
        registry=registry,
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
    )
    requests = Counter("moscowt_http_requests", "HTTP responses", ["path", "status"], registry=registry)
    queue = Gauge("moscowt_jobs", "Persisted jobs", ["status"], registry=registry)
    cache_size = Gauge("moscowt_cache_bytes", "Estimated cached response memory", registry=registry)

    @asynccontextmanager
    async def lifespan(app):
        # A small pool avoids lock convoys when many distinct requests miss the cache.
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.request_threads
        process, watch, stop = None, None, None
        if settings.worker_enabled:
            context = mp.get_context("spawn")
            stop = context.Event()

            def start_worker():
                proc = context.Process(
                    target=run_worker,
                    args=(str(store.root), str(settings.dataset_dir.resolve()), stop),
                    daemon=True,
                )
                proc.start()
                return proc

            process = start_worker()

            async def supervise():
                nonlocal process
                while True:
                    await asyncio.sleep(1)
                    if not process.is_alive():
                        process.join()
                        logging.warning(json.dumps({"event": "worker_restart", "exitcode": process.exitcode}))
                        process = start_worker()

            watch = asyncio.create_task(supervise())
        yield
        if watch:
            watch.cancel()
            try:
                await watch
            except asyncio.CancelledError:
                pass
        if process:
            stop.set()
            await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 2)

    app = FastAPI(
        title="MoscowT route-hour API",
        version="2.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/docs", include_in_schema=False)
    def api_documentation():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="MoscowT API",
            swagger_js_url="/assets/docs/swagger-ui-bundle.js",
            swagger_css_url="/assets/docs/swagger-ui.css",
            swagger_favicon_url="/favicon.svg",
            swagger_ui_parameters={"validatorUrl": None},
        )

    app.state.store, app.state.jobs, app.state.cache = store, jobs, cache
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.middleware("http")
    async def telemetry(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        elapsed = time.perf_counter() - start
        duration.labels(request.method, path).observe(elapsed)
        requests.labels(path, str(response.status_code)).inc()
        if settings.log_requests:
            logging.getLogger("moscowt.http").info(
                json.dumps(
                    {
                        "event": "http",
                        "method": request.method,
                        "path": path,
                        "status": response.status_code,
                        "durationMs": round(elapsed * 1000, 2),
                    }
                )
            )
        return response

    def snapshot(ident=None):
        result = store.read("snapshots", ident) if ident else store.current()
        if not result.get("snapshotId"):
            raise DomainError("DATA_NOT_READY", "Выполните подготовку данных", 503)
        return result

    def response(value):
        return Response(canonical(value), media_type="application/json")

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            current = store.current()
            missing = []
            for field, kind, extensions in (
                ("snapshotId", "snapshots", ("json",)),
                ("historyId", "histories", ("json", "parquet")),
                ("networkId", "networks", ("json",)),
                ("forecastId", "forecasts", ("json", "parquet")),
                *((("fleetId", "fleets", ("json",)),) if current.get("fleetId") else ()),
            ):
                ident = current.get(field)
                if not ident:
                    missing.append(field)
                    continue
                for extension in extensions:
                    if (
                        extension == "parquet"
                        and store.path(kind, ident).exists()
                        and store.read(kind, ident).get("datasetId")
                    ):
                        continue
                    path = store.path(kind, ident, extension)
                    if not path.is_file():
                        missing.append(f"{kind}/{ident}.{extension}")
                    elif extension == "json":
                        json.loads(path.read_bytes())
            if current.get("datasetId"):
                manifest = data.manifest(current["datasetId"])
                for part in manifest["partitions"]:
                    if not data.files.path("partitions", part["id"], "parquet").is_file():
                        missing.append("canonical/" + part["id"])
                run = store.read("forecast_runs", current["forecastId"])
                for kind, ident, extension in (
                    ("forecast_runs", run["id"], "parquet"),
                    ("trained_models", run["spec"]["model_id"], "json"),
                    ("trained_models", run["spec"]["model_id"], "joblib"),
                ):
                    if not store.path(kind, ident, extension).is_file():
                        missing.append(f"{kind}/{ident}.{extension}")
        except (OSError, ValueError, DomainError):
            current, missing = {}, ["invalid_artifact_manifest"]
        return JSONResponse(
            {
                "status": "not_ready" if missing else "ready",
                "missing": missing,
                "snapshotId": current.get("snapshotId"),
            },
            status_code=503 if missing else 200,
        )

    @app.get("/metrics")
    def metrics():
        with jobs.connect() as db:
            counts = dict(db.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())
        for status in ("pending", "running", "ready", "failed"):
            queue.labels(status).set(counts.get(status, 0))
        cache_size.set(cache.size)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
    def capabilities():
        current = snapshot()
        history = store.read("histories", current["historyId"]) if current.get("historyId") else None
        run = store.read("forecasts", current["forecastId"]) if current.get("forecastId") else None
        options = []
        if run:
            source = store.read("forecast_runs", run["forecastRunId"]) if run.get("forecastRunId") else {}
            options.append(
                {
                    "id": run["id"],
                    "start": run["start"],
                    "end": run["end"],
                    "origin": run["issuedAt"],
                    "kind": "annual_scenario" if source.get("model_type") == "annual_scenario" else "primary",
                    "qualityNote": source.get("quality_note", "Основной выпуск"),
                }
            )
            if current.get("datasetId"):
                from .publication import annual_options

                directory = store.root / "forecast_runs"
                options.extend(
                    o
                    for o in annual_options(
                        str(store.root),
                        current["datasetId"],
                        run["issuedAt"],
                        tuple(run["routes"]),
                        directory.stat().st_mtime_ns,
                    )
                    if o["id"] != run["id"]
                )
        return {
            "contractVersion": 2,
            "snapshotId": current["snapshotId"],
            "networkSnapshotId": current.get("networkId"),
            "metrics": [METRIC] if history else [],
            "metricScopes": ["route"],
            "unsupportedMetricScopes": ["stop", "segment", "direction", "vehicle"],
            "modes": ["auto"] + (["history"] if history else []) + (["forecast"] if run else []),
            "horizons": ["day", "month"]
            + (
                ["year"]
                if any(
                    (datetime.fromisoformat(o["end"]) - datetime.fromisoformat(o["origin"])).days >= 365
                    for o in options
                )
                else []
            )
            + (["competition_61d"] if run and run["horizon"] == "competition_61d" else []),
            "grains": ["hour", "day"],
            "exportFormats": ["csv"],
            "stream": False,
            "ingestion": True,
            "timezone": "Europe/Moscow",
            "targetRouteIds": sorted(set(history.get("routes", [str(r) for r in ROUTES]))) if history else [],
            "historyRange": {"start": history["start"], "end": history["end"]} if history else None,
            "forecastRange": {"start": run["start"], "end": run["end"]} if run else None,
            "forecastId": run["id"] if run else None,
            "forecastOptions": options,
            "fleetId": current.get("fleetId"),
            "vehicleLoadThresholds": VEHICLE_LOAD_THRESHOLDS,
            "currentTime": history["end"] if history else None,
            "defaultDate": run["start"][:10] if run else history["start"][:10],
            # Historical mean of positive route/day hourly maxima: 2077.5.
            "absoluteThresholds": [200, 600, 1200, 2000],
            "uncertaintyIntervals": False,
            "submissionAvailable": bool(
                run and run["start"].startswith("2025-11-01") and run["rows"] == 14640
            ),
        }

    def decorated_network(current, date=None):
        selected = resolve_network(store, store.read("networks", current["networkId"]), date)
        value = {**selected, "routes": [dict(route) for route in selected["routes"]]}
        if current.get("datasetId"):
            known = set(data.manifest(current["datasetId"])["routes"])
            definitions = {r["id"]: r for r in data.catalog.routes()}
            present = {r["id"] for r in value["routes"]}
            for rid in sorted(known - present):
                definition = definitions.get(rid, {})
                value["routes"].append(
                    {
                        "id": rid,
                        "number": definition.get("number", rid),
                        "name": definition.get("name", "Маршрут " + rid),
                        "color": "#167f78",
                        "isTarget": True,
                        "hasData": True,
                        "hasGeometry": False,
                        "historySupport": "available",
                    }
                )
            geometry = {p["routeId"] for p in value["patterns"]}
            for route in value["routes"]:
                route["hasData"] = route["id"] in known
                route["historySupport"] = "available" if route["hasData"] else "unavailable"
            value["missingGeometryRouteIds"] = sorted(known - geometry)
        return value

    @app.get("/api/v1/network", response_model=NetworkResponse)
    def get_network(snapshotId: str | None = None, date: CalendarDate | None = None):
        current = snapshot(snapshotId)
        if not current.get("networkId"):
            raise DomainError("NETWORK_NOT_READY", "Справочник не подготовлен", 503)
        value = decorated_network(current, date.isoformat() if date else None)
        return response(value)

    @app.post("/api/v1/geometry", response_model=GeometrySelection)
    def get_geometry(scope: Scope):
        return response(network.geometry(scope))

    @app.post("/api/v1/route-imports/preview")
    def preview_route(body: RouteImportRequest):
        return preview_import(store, body)

    @app.post("/api/v1/route-imports/{ident}/apply")
    def apply_route(ident: str, body: ApplyImportRequest):
        return apply_import(store, ident, body.snapshotId)

    @app.get("/api/v1/routes", response_model=list[Route])
    def get_routes(snapshotId: str | None = None, date: CalendarDate | None = None):
        current = snapshot(snapshotId)
        return decorated_network(current, date.isoformat() if date else None)["routes"]

    @app.get("/api/v1/forecast-runs")
    def forecast_runs():
        return [json.loads(p.read_bytes()) for p in sorted((store.root / "forecasts").glob("*.json"))]

    @app.get("/api/v1/data-quality")
    def data_quality(snapshotId: str | None = None):
        current = snapshot(snapshotId)
        fleet = store.read("fleets", current["fleetId"]) if current.get("fleetId") else {}
        return {
            "snapshotId": current["snapshotId"],
            "manifest": store.read("histories", current["historyId"]),
            "anomalies": [
                {"routeId": "50", "dates": ["2025-09-20", "2025-09-21"], "status": "requires_review"}
            ],
            "route5Policy": "no_forced_zero; see_model_route_coverage"
            if current.get("datasetId")
            else "zero_fallback_no_positive_history",
            "fleetId": current.get("fleetId"),
            "fleetMethod": fleet.get("method"),
            "limitations": [
                "Остановочного target и наполнения вагонов нет",
                "География зависит от выбранной даты и полноты архива OSM",
                "Число вагонов оценивается по событиям: вагоны без валидаций могут быть пропущены",
                "Профиль вагонов строится по 8 предшествующим неделям; тип дня по производственному календарю является допущением",
                "Полный архив расписаний на 2025 год не найден; справочные интервалы не подменяют ежедневный выпуск",
                "Поток событий и SSE не включены; задания доступны через поллинг",
            ],
        }

    @app.get("/api/v1/service-dataset-2025/{filename}")
    def service_dataset(filename: str):
        available = {
            "daily_service_2025.csv": "text/csv",
            "hourly_fleet_2025.csv": "text/csv",
            "archived_stop_departures.csv": "text/csv",
            "sources.json": "application/json",
            "summary.json": "application/json",
            "README.md": "text/markdown",
        }
        if filename not in available:
            raise DomainError("DATASET_FILE_NOT_FOUND", "Файл датасета не найден", 404)
        path = settings.dataset_dir / "derived/schedules-2025" / filename
        if not path.is_file():
            raise DomainError("DATASET_NOT_READY", "Датасет расписаний ещё не подготовлен", 404)
        return FileResponse(path, filename=filename, media_type=available[filename])

    @app.post("/api/v1/map-snapshot", response_model=SnapshotResponse)
    def map_snapshot(scope: Scope):
        geometry = network.geometry(scope)
        view = cache.get(scope)
        return response({"meta": view["meta"], "frames": view["frames"], "geometry": geometry})

    @app.post("/api/v1/timeseries", response_model=SeriesResponse)
    def timeseries(scope: Scope):
        network.geometry(scope)
        view = cache.get(scope)
        return response({"meta": view["meta"], "series": view["series"]})

    @app.post("/api/v1/route-comparison", response_model=ComparisonResponse)
    def comparison(scope: Scope):
        network.geometry(scope)
        view = cache.get(scope)
        return response({"meta": view["meta"], "routes": view["comparison"]})

    @app.post("/api/v1/heatmap", response_model=HeatmapResponse)
    def heatmap(scope: Scope):
        network.geometry(scope)
        view = cache.get(scope)
        return response({"meta": view["meta"], **view["heatmap"]})

    @app.post("/api/v1/route-profile", status_code=422)
    def route_profile(scope: Scope):
        raise DomainError(
            "METRIC_GRAIN_UNAVAILABLE", "Остановочный профиль отсутствует; используйте сравнение маршрутов"
        )

    @app.post("/api/v1/exports", status_code=202, response_model=JobResponse)
    def exports(body: ExportRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        network.geometry(body.scope)
        view = cache.get(body.scope)
        if body.index is not None and body.index >= len(view["frames"]):
            raise DomainError("INVALID_FRAME", "Временной интервал отсутствует")
        return jobs.enqueue("export", body.model_dump(mode="json"), idempotency_key)

    @app.post("/api/v1/submissions", status_code=202, response_model=JobResponse)
    def submissions(body: SubmissionRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        validate_submission_run(store, body.snapshotId)
        return jobs.enqueue("submission", body.model_dump(mode="json"), idempotency_key)

    @app.get("/api/v1/jobs/{ident}", response_model=JobResponse)
    @app.get("/api/v1/exports/{ident}", response_model=JobResponse)
    def get_job(ident: str):
        return jobs.public(jobs.get(ident))

    @app.get("/api/v1/exports/{ident}/download")
    def download(ident: str):
        job = jobs.get(ident)
        if job["status"] != "ready":
            raise DomainError("EXPORT_NOT_READY", "Файл ещё не готов", 409)
        return FileResponse(
            store.root / "exports" / job["filename"],
            media_type="text/csv; charset=utf-8",
            filename="submission.csv" if job["kind"] == "submission" else "validations.csv",
        )

    from .platform_api import router

    app.include_router(router(settings, data, store, cache))
    from .passenger_api import router as passenger_router

    app.include_router(passenger_router(settings, store, data))

    if settings.frontend_dir.is_dir():
        assets = settings.frontend_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        def frontend():
            return FileResponse(settings.frontend_dir / "index.html")

        @app.get("/favicon.svg")
        def favicon():
            return FileResponse(settings.frontend_dir / "favicon.svg")

    return app
