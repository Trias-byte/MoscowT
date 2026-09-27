"""Restore bundled data once and install versioned model and passenger releases."""

import logging
import os
import tarfile
from pathlib import Path

from .bundles import restore_bundle
from .config import Settings
from .constants.runtime import SERVER_COMMAND
from .domain import DomainError


class RuntimeBootstrap:
    def __init__(self, settings: Settings):
        self.settings = settings

    def prepare(self):
        if self.settings.demo and not (self.settings.state_dir / "current.json").exists():
            restore_bundle(self.settings.demo_bundle, self.settings)
        self.install_passengers()
        self.install_baseline()

    def install_passengers(self):
        from .passenger_package import install_package

        package = Path(os.getenv("MOSCOWT_PASSENGER_PACKAGE", str(self.settings.passenger_package)))
        if package.is_file():
            try:
                install_package(self.settings.data_root, package, bundled=True)
            except (DomainError, OSError, ValueError, tarfile.TarError):
                logging.exception("Passenger release could not be installed; previous pointer retained")

    def install_baseline(self):
        from .modeling.baseline_install import install_baseline

        package = self.settings.baseline_package
        if package.is_file():
            try:
                install_baseline(self.settings, package, bundled=True)
            except (DomainError, OSError, ValueError):
                logging.exception("Baseline could not be installed; previous forecast retained")


def prepare_demo(settings: Settings):
    RuntimeBootstrap(settings).prepare()


def install_passengers(settings: Settings):
    RuntimeBootstrap(settings).install_passengers()


def main():
    RuntimeBootstrap(Settings()).prepare()
    os.execvp("moscowt", ["moscowt", *SERVER_COMMAND])


if __name__ == "__main__":
    main()
