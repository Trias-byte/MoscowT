"""Run the standard 300 RPS workload while the dedicated worker trains another model."""

import argparse
import json
import subprocess
import sys
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
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    snapshot = request("/api/v2/capabilities")["current_snapshot"]
    run = request("/api/v2/forecast-runs/" + snapshot["forecastId"])
    model = next(m for m in request("/api/v2/models") if m["id"] == run["spec"]["model_id"])
    spec = {
        **model["spec"],
        "seed": uuid.uuid4().int % (2**31 - 1),
        "purpose": "research",
        "parameters": {"cat_iterations": 1378, "lgb_estimators": 332, "rf_estimators": 300},
    }
    task = request("/api/v2/training-runs", spec)
    deadline = time.monotonic() + 60
    while task["status"] == "pending":
        time.sleep(0.2)
        task = request("/api/v2/jobs/" + task["id"])
        assert time.monotonic() < deadline, task
    assert task["status"] == "running", task
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("load_test.py")),
                "--url",
                args.url,
                "--container",
                args.container,
                "--mode",
                "warm",
                "--rps",
                "300",
                "--seconds",
                "30",
                "--exports",
                "--output",
                args.output,
            ],
            check=True,
        )
        status_after = request("/api/v2/jobs/" + task["id"])
    finally:
        request("/api/v2/jobs/" + task["id"] + "/cancel", {})
    report = json.loads(Path(args.output).read_bytes())
    report["concurrent_training"] = {
        "id": task["id"],
        "model_type": spec["model_type"],
        "seed": spec["seed"],
        "parameters": spec["parameters"],
        "status_at_start": task["status"],
        "status_at_end": status_after["status"],
        "container_memory_limit_bytes": json.loads(
            subprocess.check_output(["docker", "inspect", "--format", "{{json .HostConfig}}", args.container])
        )["Memory"],
    }
    report["snapshot_unchanged"] = request("/api/v2/capabilities")["current_snapshot"] == snapshot
    Path(args.output).write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", default="docs/performance-v2-training.json")
    main(parser.parse_args())
