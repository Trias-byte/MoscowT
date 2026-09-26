"""Exercise an interrupted export worker in the explicitly selected local container."""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx


def signal_worker(container, signal):
    code = """
import os, pathlib, signal, sys
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
        if len(args) > 2 and args[1] == b'-c' and args[2].startswith(b'from multiprocessing.spawn import spawn_main;'):
            pid = int(path.parent.name)
            os.kill(pid, getattr(signal, sys.argv[1]))
            print(pid)
            break
    except (FileNotFoundError, ProcessLookupError):
        pass
else:
    raise RuntimeError('No export worker found')
"""
    return int(subprocess.check_output(["docker", "exec", container, "python", "-c", code, signal]))


def main(args):
    key = "recovery-" + uuid.uuid4().hex
    with httpx.Client(base_url=args.url, timeout=20) as client:
        cap = client.get("/api/v1/capabilities").json()
        body = {
            "scope": {
                "routeIds": cap["targetRouteIds"],
                "snapshotId": cap["snapshotId"],
                "mode": "history",
                "timeRange": {"start": "2025-01-01T00:00:00+03:00", "end": "2025-03-03T00:00:00+03:00"},
            }
        }
        stopped_pid = signal_worker(args.container, "SIGSTOP")
        try:
            r = client.post("/api/v1/exports", json=body, headers={"Idempotency-Key": key})
            r.raise_for_status()
            job = r.json()
            assert job["status"] == "pending"
            repeated = client.post("/api/v1/exports", json=body, headers={"Idempotency-Key": key}).json()
            assert repeated["id"] == job["id"]
        finally:
            signal_worker(args.container, "SIGCONT")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            running = client.get("/api/v1/jobs/" + job["id"]).json()
            if running["status"] == "running":
                break
            assert running["status"] == "pending", running
            time.sleep(0.005)
        else:
            raise AssertionError("Worker did not claim the job")
        killed_pid = signal_worker(args.container, "SIGKILL")
        assert stopped_pid == killed_pid
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = client.get("/api/v1/jobs/" + job["id"]).json()
            assert result["status"] != "failed", result
            if result["status"] == "ready":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Acknowledged export was not recovered")
        content = client.get(result["downloadUrl"]).content
        assert len(content.decode("utf-8-sig").splitlines()) == 14641
        report = {
            "snapshotId": cap["snapshotId"],
            "jobId": job["id"],
            "statusBeforeKill": running["status"],
            "statusAfterRecovery": result["status"],
            "killedWorkerPid": killed_pid,
            "idempotencyVerified": True,
            "rows": 14640,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        # Also check that a full container restart preserves the acknowledged job and file.
        subprocess.run(["docker", "restart", args.container], check=True, capture_output=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if client.get("/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        restored = client.get("/api/v1/jobs/" + job["id"]).json()
        assert restored["status"] == "ready"
        assert client.get(restored["downloadUrl"]).content == content
        report["containerRestartPreservedFile"] = True
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", required=True)
    main(parser.parse_args())
