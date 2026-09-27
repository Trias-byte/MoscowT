"""Kill and cancel a model worker in an explicitly selected isolated Compose project."""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


def main(args):
    def request(path, body=None):
        req = Request(
            args.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex},
        )
        with urlopen(req, timeout=20) as response:
            return json.load(response)

    cap = request("/api/v2/capabilities")
    snapshot = cap["current_snapshot"]
    run = request("/api/v2/forecast-runs/" + snapshot["forecastId"])
    models = request("/api/v2/models")
    model = next(m for m in models if m["id"] == run["spec"]["model_id"])
    scope = {
        "snapshotId": snapshot["snapshotId"],
        "routeIds": ["17"],
        "timeRange": {"start": "2025-11-01T00:00:00+03:00", "end": "2025-11-02T00:00:00+03:00"},
    }

    def fingerprint():
        payload = request("/api/v1/map-snapshot", scope)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    before = fingerprint()
    spec = {
        **model["spec"],
        "seed": 98765,
        "parameters": {"cat_iterations": 4000, "lgb_estimators": 4000, "rf_estimators": 1000},
    }
    job = request("/api/v2/training-runs", spec)
    deadline = time.monotonic() + 180
    reads = 0
    while job["status"] == "pending":
        time.sleep(0.5)
        job = request("/api/v2/jobs/" + job["id"])
        assert time.monotonic() < deadline, job
    assert job["status"] == "running", job
    container = subprocess.check_output(
        ["docker", "compose", "-p", args.project, "ps", "-q", "model-worker"], text=True
    ).strip()
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            "import os,signal; from pathlib import Path; "
            "[os.kill(int(p.name),signal.SIGKILL) for p in Path('/proc').iterdir() if p.name.isdigit() and p.name!='1' "
            "and (p/'cmdline').exists() and b'worker' in (args:=(p/'cmdline').read_bytes().split(bytes([0]))) "
            "and any(a.endswith(b'/moscowt') for a in args)]",
        ],
        check=True,
        capture_output=True,
    )
    while job["attempts"] < 2:
        time.sleep(1)
        assert fingerprint() == before
        reads += 1
        job = request("/api/v2/jobs/" + job["id"])
        assert time.monotonic() < deadline, job
    request("/api/v2/jobs/" + job["id"] + "/cancel", {})
    while job["status"] in ("running", "pending"):
        time.sleep(0.5)
        job = request("/api/v2/jobs/" + job["id"])
        assert time.monotonic() < deadline, job
    assert job["status"] == "cancelled", job
    assert request("/api/v2/capabilities")["current_snapshot"] == snapshot
    assert fingerprint() == before
    assert {m["id"] for m in request("/api/v2/models")} == {m["id"] for m in models}
    subprocess.run(
        ["docker", "compose", "-p", args.project, "restart", "api", "model-worker"],
        check=True,
        capture_output=True,
    )
    while True:
        try:
            if request("/health/ready")["status"] == "ready":
                break
        except OSError:
            pass
        time.sleep(0.5)
        assert time.monotonic() < deadline
    assert fingerprint() == before
    repeated = request("/api/v2/forecast-runs", run["spec"])
    while repeated["status"] in ("running", "pending"):
        time.sleep(0.5)
        repeated = request("/api/v2/jobs/" + repeated["id"])
        assert time.monotonic() < deadline
    assert repeated["status"] == "ready", repeated
    assert repeated["result"]["sha256"] == run["sha256"]
    recompute = """
import json
import numpy as np
from moscowt.config import Settings
from moscowt.storage import SnapshotStore
from moscowt.data.repository import DatasetRepository
from moscowt.modeling.service import ModelService
from moscowt.modeling.contracts import ForecastSpec, TrainingSpec
from moscowt.modeling.adapters import ADAPTERS
s=Settings(); data=DatasetRepository(s.data_root,read_only=True); store=SnapshotStore(s.state_dir)
service=ModelService(data,store); demo=json.loads((store.root/'demo.json').read_bytes()); checked=[]
for ident in demo['forecasts']:
    run, saved=service.frame(ident); spec=ForecastSpec.model_validate(run['spec'])
    manifest,model=service.load(spec.model_id); training=TrainingSpec.model_validate(manifest['spec'])
    adapter=ADAPTERS[training.model_type]
    history=service._history(spec.dataset_id,spec.route_ids,training.time_range.start,spec.origin,adapter.minimum_days)
    fresh=adapter.predict(model,history,spec)
    np.testing.assert_array_equal(saved.value,fresh.value)
    checked.append(training.model_type)
print(json.dumps(checked))
"""
    checked = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "-p",
                args.project,
                "exec",
                "-T",
                "model-worker",
                "python",
                "-c",
                recompute,
            ],
            text=True,
        )
    )
    report = {
        "job": job,
        "successful_reads_during_recovery": reads,
        "snapshot_unchanged": True,
        "no_partial_model_published": True,
        "restart_forecast_checksum_equal": True,
        "recomputed_after_reload": checked,
        "created_at": time.time(),
    }
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", default="docs/worker-recovery.json")
    main(parser.parse_args())
