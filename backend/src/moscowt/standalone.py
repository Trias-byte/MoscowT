"""Run the API and queue workers with one container lifecycle."""

import signal
import subprocess
import time
from collections.abc import Sequence

from .config import Settings
from .constants.runtime import PROCESS_COMMANDS, PROCESS_POLL_SECONDS, SHUTDOWN_TIMEOUT_SECONDS
from .entrypoint import RuntimeBootstrap


class ProcessSupervisor:
    def __init__(
        self,
        commands: Sequence[Sequence[str]] = PROCESS_COMMANDS,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
    ):
        self.commands = commands
        self.shutdown_timeout = shutdown_timeout
        self.children: list[subprocess.Popen] = []
        self.stopping = False

    def stop(self, signum=signal.SIGTERM, _frame=None):
        self.stopping = True
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signum)

    def shutdown(self):
        self.stop()
        deadline = time.monotonic() + self.shutdown_timeout
        for child in self.children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    def run(self) -> int:
        previous = {sig: signal.signal(sig, self.stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            for command in self.commands:
                if self.stopping:
                    break
                self.children.append(subprocess.Popen(["moscowt", *command]))
            while not self.stopping:
                for child in self.children:
                    if child.poll() is not None:
                        return child.returncode or 1
                time.sleep(PROCESS_POLL_SECONDS)
            return 0
        finally:
            self.shutdown()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    RuntimeBootstrap(Settings()).prepare()
    raise SystemExit(ProcessSupervisor().run())


if __name__ == "__main__":
    main()
