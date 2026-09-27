"""Download a full HTTP export and restore it into empty temporary directories."""

import argparse
import hashlib
import json
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient

from moscowt.api import create_app
from moscowt.bundles import restore_bundle
from moscowt.config import Settings


def main(args):
    def request(path, body=None):
        req = Request(
            args.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex},
        )
        with urlopen(req, timeout=60) as response:
            return json.load(response)

    started = datetime.now(timezone.utc).isoformat()
    task = request("/api/v2/bundles", {})
    deadline = time.monotonic() + 180
    while task["status"] in ("pending", "running"):
        assert time.monotonic() < deadline, task
        time.sleep(0.5)
        task = request("/api/v2/jobs/" + task["id"])
    assert task["status"] == "ready", task
    with tempfile.TemporaryDirectory(prefix="potok-bundle-") as directory:
        root = Path(directory)
        archive = root / "artifacts.tar.gz"
        checksum = hashlib.sha256()
        with (
            urlopen(args.url + task["result"]["download_url"], timeout=120) as response,
            archive.open("wb") as output,
        ):
            while content := response.read(1024 * 1024):
                checksum.update(content)
                output.write(content)
        assert checksum.hexdigest() == task["result"]["sha256"]
        settings = Settings(state_dir=root / "state", data_root=root / "data", worker_enabled=False)
        result = restore_bundle(archive, settings)
        with TestClient(create_app(settings)) as client:
            assert client.get("/health/ready").status_code == 200
            cap = client.get("/api/v2/capabilities").json()
            current = cap["current_snapshot"]
            run = client.get("/api/v2/forecast-runs/" + current["forecastId"]).json()
            start = datetime.fromisoformat(run["spec"]["time_range"]["start"])
            query = {
                "dataset_id": run["spec"]["dataset_id"],
                "forecast_id": run["id"],
                "route_ids": run["spec"]["route_ids"],
                "mode": "forecast",
                "time_range": {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat()},
            }
            response = client.post("/api/v2/forecasts/query", json=query)
            assert response.status_code == 200, response.text
            expected = request("/api/v2/forecasts/query", query)
            assert response.json() == expected
        report = {
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "url": args.url,
            "job_id": task["id"],
            "sha256": checksum.hexdigest(),
            "bytes": archive.stat().st_size,
            **result,
            "ready_after_restore": True,
            "forecast_rows_equal": len(expected["rows"]),
            "snapshot_id": current["snapshotId"],
        }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", default="docs/bundle-restore.json")
    main(parser.parse_args())
