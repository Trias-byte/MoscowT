import fcntl
import logging
import multiprocessing
import signal
import threading
import uuid

from .config import Settings
from .data.repository import DatasetRepository
from .data.schemas import ImportSpec
from .jobs import JobRepository
from .modeling.contracts import ForecastSpec, TrainingSpec
from .modeling.service import ModelService
from .schedules import ScheduleService
from .storage import SnapshotStore
from .tasks import GENERAL_KINDS, MODEL_KINDS, WorkQueue


def execute(settings: Settings, job):
    store = SnapshotStore(settings.state_dir)
    data = DatasetRepository(settings.data_root, read_only=job["kind"] in MODEL_KINDS)
    models = ModelService(data, store, settings.model_threads, settings.max_forecast_hours)
    payload = job["payload"]
    if job["kind"] == "weather_fetch":
        from .external import OpenMeteoForecastProvider

        return OpenMeteoForecastProvider(settings.data_root).fetch()
    if job["kind"] == "train":
        return models.train(TrainingSpec.model_validate(payload))
    if job["kind"] == "forecast":
        return models.predict(ForecastSpec.model_validate(payload))
    if job["kind"] == "import_preview":
        source = store.path("uploads", payload["blob_id"], "csv")
        return data.preview([source], ImportSpec.model_validate(payload["spec"]))
    if job["kind"] == "import_apply":
        return data.apply(payload["upload_id"])
    if job["kind"] == "schedule_import":
        return ScheduleService(settings.data_root).import_file(
            store.path("uploads", payload["blob_id"], "csv")
        )
    if job["kind"] == "period_export":
        from .platform_exports import PeriodExporter, PeriodExportSpec

        return PeriodExporter(settings, data, models).write(
            PeriodExportSpec.model_validate(payload), job["id"]
        )
    if job["kind"] == "bundle_export":
        from .bundles import create_bundle

        return create_bundle(settings, job["id"])
    raise ValueError("Unknown work kind")


def execute_and_finish(settings, job):
    queue = WorkQueue(settings.state_dir, settings.queue_limit)
    try:
        result = execute(settings, job)
        queue.finish(job["id"], job["owner"], result=result)
    except Exception as exc:
        logging.exception("Platform job %s failed", job["id"])
        queue.finish(job["id"], job["owner"], error=str(exc)[:2000])


def run(settings: Settings, channel: str, stop=None):
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    store = SnapshotStore(settings.state_dir)
    queue = WorkQueue(store.root, settings.queue_limit)
    owner = uuid.uuid4().hex
    kinds = MODEL_KINDS if channel == "model" else GENERAL_KINDS
    with (store.root / f".platform-{channel}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        legacy = JobRepository(store.root) if channel == "general" else None
        if legacy:
            legacy.recover()
        while not stop.is_set():
            job = queue.claim(kinds, owner)
            if job:
                process = multiprocessing.get_context("spawn").Process(
                    target=execute_and_finish, args=(settings, job)
                )
                process.start()
                while process.is_alive() and not stop.wait(0.5):
                    if not queue.heartbeat(job["id"], owner):
                        process.terminate()
                        break
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
                if queue.get(job["id"])["status"] == "running":
                    queue.finish(job["id"], owner, error="Worker interrupted; submit again to retry")
            elif legacy and (old := legacy.claim()):
                from .worker import process_job

                process_job(
                    store,
                    legacy,
                    settings.source_dir if settings.source_dir.exists() else settings.dataset_dir,
                    old,
                )
            else:
                stop.wait(0.5)
