"""Restore the bundled demo once; normal starts never retrain or fetch external data."""

import os
from pathlib import Path

from .bundles import restore_bundle
from .config import Settings


def prepare_demo(settings):
    bundle = Path("/app/demo.tar.gz")
    if (
        os.getenv("MOSCOWT_DEMO", "false").lower() == "true"
        and not (settings.state_dir / "current.json").exists()
    ):
        restore_bundle(bundle, settings)


def main():
    prepare_demo(Settings())
    os.execvp("moscowt", ["moscowt", "serve", "--host", "0.0.0.0", "--port", "8000"])


if __name__ == "__main__":
    main()
