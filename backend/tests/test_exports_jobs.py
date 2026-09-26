import io

import pandas as pd
import pytest

from moscowt.domain import ExportRequest
from moscowt.exports import SubmissionService, user_csv
from moscowt.jobs import JobRepository
from moscowt.worker import process_job


def test_submission_complete_reproducible_ignores_sample(store, dataset, tmp_path):
    service = SubmissionService(store, dataset)
    snapshot = store.current()["snapshotId"]
    data = service.build(snapshot)
    frame = pd.read_csv(io.BytesIO(data), sep=";")
    assert list(frame) == ["route", "date", "hour", "prediction"] and len(frame) == 14640
    assert not frame.duplicated(["route", "date", "hour"]).any()
    assert len(frame.route.unique()) == 10 and frame[frame.route == 5].prediction.sum() == 0
    assert frame.prediction.min() >= 0 and frame.prediction.dtype.kind in "iu"
    template = pd.read_csv(dataset / "test_submission.csv", sep=";")
    template.prediction = 999999
    template.to_csv(tmp_path / "test_submission.csv", sep=";", index=False)
    assert SubmissionService(store, tmp_path).build(snapshot) == data
    assert service.build(snapshot) == data
    template.iloc[:-1].to_csv(tmp_path / "test_submission.csv", sep=";", index=False)
    with pytest.raises(ValueError, match="template"):
        SubmissionService(store, tmp_path).build(snapshot)


def test_user_export_includes_routes_without_geography(store, scope):
    scope["routeIds"] = ["5", "17", "50"]
    exported = user_csv(store, ExportRequest.model_validate({"scope": scope, "index": 8}))
    frame = pd.read_csv(io.BytesIO(exported), sep=";")
    assert set(frame.route) == {5, 17, 50} and len(frame) == 3
    assert not frame.geometry_filter_affects_metric.any()


def test_mixed_export_keeps_source_per_row(store, scope):
    scope.update(
        mode="auto",
        routeIds=["1"],
        timeRange={
            "start": "2025-10-31T23:00:00+03:00",
            "end": "2025-11-01T01:00:00+03:00",
        },
    )
    frame = pd.read_csv(io.BytesIO(user_csv(store, ExportRequest.model_validate({"scope": scope}))), sep=";")
    assert frame.provenance.tolist() == ["observation", "forecast"]
    assert pd.isna(frame.forecast_id.iloc[0])
    assert pd.notna(frame.forecast_id.iloc[1])


def test_queue_idempotence_conflict_and_backpressure(tmp_path):
    repo = JobRepository(tmp_path, limit=1)
    first = repo.enqueue("export", {"x": 1}, "key")
    assert repo.enqueue("export", {"x": 1}, "key")["id"] == first["id"]
    with pytest.raises(Exception, match="другим запросом"):
        repo.enqueue("export", {"x": 2}, "key")
    with pytest.raises(Exception, match="Очередь"):
        repo.enqueue("export", {"x": 2}, "key2")
    job = repo.claim()
    assert job["id"] == first["id"]
    restarted = JobRepository(tmp_path, limit=1)
    restarted.recover()
    assert restarted.get(first["id"])["status"] == "pending"
    assert restarted.claim()["id"] == first["id"]


def test_confirmed_job_survives_worker_interruption(store, dataset, scope):
    repo = JobRepository(store.root)
    job = repo.enqueue("export", {"scope": scope, "index": 0}, "restart-test")
    claimed = repo.claim()
    assert claimed["id"] == job["id"]
    repo.recover()
    claimed = repo.claim()
    process_job(store, repo, dataset, claimed)
    record = repo.get(job["id"])
    assert record["status"] == "ready"
    assert (store.root / "exports" / record["filename"]).is_file()


def test_api_export_job_and_download(client, scope, dataset):
    r = client.post(
        "/api/v1/exports", json={"scope": scope, "index": 8}, headers={"Idempotency-Key": "http-job"}
    )
    assert r.status_code == 202
    store = client.app.state.store
    repo = client.app.state.jobs
    process_job(store, repo, dataset, repo.claim())
    status = client.get("/api/v1/jobs/" + r.json()["id"]).json()
    assert status["status"] == "ready"
    content = client.get(status["downloadUrl"])
    assert content.status_code == 200 and "snapshot_id" in content.text
    # Submission body cannot accept map filters.
    invalid = client.post(
        "/api/v1/submissions",
        json={"snapshotId": scope["snapshotId"], "routeIds": ["1"]},
        headers={"Idempotency-Key": "bad-submission"},
    )
    assert invalid.status_code == 422
