import fcntl
import logging
import multiprocessing
import signal
import threading
import uuid

from .config import Settings
from .job_executor import JobExecutor
from .jobs import JobRepository
from .storage import SnapshotStore
from .tasks import GENERAL_KINDS, MODEL_KINDS, WorkQueue


def execute(settings: Settings, job):
    return JobExecutor(settings, job).execute()


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
                    if job["kind"] == "model_refresh":
                        queue.interrupted(job["id"], owner)
                    else:
                        queue.finish(job["id"], owner, error="Worker interrupted; submit again to retry")
            # Give the compatibility export queue a turn even under continuous
            # v2 ingestion, without letting either queue starve the other.
            if not stop.is_set() and legacy and (old := legacy.claim()):
                from .worker import process_job

                process_job(
                    store,
                    legacy,
                    settings.source_dir if settings.source_dir.exists() else settings.dataset_dir,
                    old,
                )
            elif not job:
                stop.wait(0.5)
