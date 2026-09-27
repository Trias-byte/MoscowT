"""API and both queue channels under one container's resource limits.

Exit as a group on a child failure so Docker restarts the service; persisted job
leases recover in WorkQueue. All children receive graceful shutdown signals.
"""

import signal
import subprocess
import time

from .config import Settings
from .entrypoint import prepare_demo


def main():
    prepare_demo(Settings())
    children = []
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    result = 0
    try:
        for args in (
            ["serve", "--host", "0.0.0.0", "--port", "8000"],
            ["worker", "--channel", "general"],
            ["worker", "--channel", "model"],
        ):
            children.append(subprocess.Popen(["moscowt", *args]))
        while not stopping:
            failed = next((child for child in children if child.poll() is not None), None)
            if failed is not None:
                result = failed.returncode or 1
                break
            time.sleep(0.2)
    finally:
        stop(signal.SIGTERM, None)
        deadline = time.monotonic() + 20
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    raise SystemExit(result)


if __name__ == "__main__":
    main()
