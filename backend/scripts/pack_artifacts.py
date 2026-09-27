"""Create a portable archive and Git-friendly <=64 MiB demo chunks."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from moscowt.bundles import create_bundle
from moscowt.config import Settings
from moscowt.storage import file_hash, write_json

settings = Settings()
# The shipped demo has the three selected recipes. Research runs remain in the
# live workspace and its full /bundles export, not in every clean installation.
demo = json.loads((settings.state_dir / "demo.json").read_bytes())
with tempfile.TemporaryDirectory() as temporary:
    staging = Path(temporary) / "state"
    staging.mkdir()
    for kind in ("networks", "route_imports", "evaluations", "reports"):
        source_dir = settings.state_dir / kind
        if source_dir.exists():
            shutil.copytree(source_dir, staging / kind)
    selected = {
        "trained_models": demo["models"],
        "forecast_runs": demo["forecasts"],
        "forecasts": [demo["snapshot"]["forecastId"]],
        "histories": [demo["snapshot"]["historyId"]],
        "snapshots": [demo["snapshot"]["snapshotId"]],
        "fleets": [demo["snapshot"]["fleetId"]],
    }
    for kind, identifiers in selected.items():
        (staging / kind).mkdir(exist_ok=True)
        for ident in identifiers:
            for path in (settings.state_dir / kind).glob(ident + ".*"):
                shutil.copyfile(path, staging / kind / path.name)
    for name in ("current.json", "demo.json"):
        shutil.copyfile(settings.state_dir / name, staging / name)
    delivery = settings.model_copy(update={"state_dir": staging})
    result = create_bundle(delivery, "portable")
    (settings.state_dir / "exports").mkdir(exist_ok=True)
    shutil.copyfile(
        staging / "exports" / result["filename"], settings.state_dir / "exports" / result["filename"]
    )
source = settings.state_dir / "exports" / result["filename"]
output = Path(__file__).resolve().parents[2] / "deliverables"
output.mkdir(exist_ok=True)
for old in output.glob("platform-demo.tar.gz.part-*"):
    old.unlink()
parts = []
with source.open("rb") as stream:
    index = 0
    while content := stream.read(64 * 1024 * 1024):
        path = output / f"platform-demo.tar.gz.part-{index:03d}"
        path.write_bytes(content)
        parts.append(
            {"path": path.name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
        index += 1
write_json(output / "platform-demo.manifest.json", {"sha256": file_hash(source), "parts": parts})
print(json.dumps({"archive": str(source), "parts": len(parts), "bytes": source.stat().st_size}))
