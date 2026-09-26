import fcntl
import json
import logging
from pathlib import Path

from .domain import ExportRequest
from .exports import SubmissionService, user_csv
from .jobs import JobRepository
from .storage import SnapshotStore, atomic_write


def process_job(store, repository, dataset, job):
    try:
        if job["kind"] == "submission":
            content = SubmissionService(store, dataset).build(job["payload"]["snapshotId"])
        else:
            content = user_csv(store, ExportRequest.model_validate(job["payload"]))
        filename = f"{job['id']}.csv"
        atomic_write(store.root / "exports" / filename, content)
        repository.finish(job["id"], filename)
    except Exception as exc:
        logging.exception(json.dumps({"event": "job_failed", "jobId": job["id"]}))
        repository.finish(job["id"], error=str(exc)[:1000])


def run_worker(root: str, dataset: str, stop):
    store = SnapshotStore(Path(root))
    with (store.root / ".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        repository = JobRepository(store.root)
        repository.recover()
        while not stop.is_set():
            job = repository.claim()
            if job:
                process_job(store, repository, Path(dataset), job)
            else:
                stop.wait(0.2)
