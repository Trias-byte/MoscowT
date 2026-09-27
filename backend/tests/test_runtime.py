import signal
import subprocess
import sys

from moscowt.config import Settings
from moscowt.constants.config import PROJECT_ROOT
from moscowt.standalone import ProcessSupervisor


def test_default_input_paths_stay_inside_project(tmp_path):
    assert Settings().source_dir == PROJECT_ROOT / "dataset"
    assert Settings(dataset_dir=tmp_path).source_dir == tmp_path


def test_supervisor_stops_other_children_after_failure(monkeypatch):
    original = subprocess.Popen
    children = []

    def launch(args):
        process = original([sys.executable, *args[1:]])
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    previous = signal.getsignal(signal.SIGTERM)
    supervisor = ProcessSupervisor(
        commands=(("-c", "import time; time.sleep(60)"), ("-c", "raise SystemExit(7)")),
        shutdown_timeout=1,
    )
    assert supervisor.run() == 7
    assert all(child.poll() is not None for child in children)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_supervisor_escalates_when_child_ignores_shutdown():
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready'); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
    )
    supervisor = ProcessSupervisor(shutdown_timeout=0.1)
    supervisor.children.append(process)
    try:
        assert process.stdout.readline() == b"ready\n"
        supervisor.shutdown()
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
