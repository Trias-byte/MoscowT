"""Package immutable artifacts only, excluding runtime DB, locks, logs and jobs."""

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
source = root / "artifacts"
out = root / "deliverables"
out.mkdir(exist_ok=True)
files = [source / "current.json", source / "backtest.json", source / "submission.csv"]
for directory in ["histories", "networks", "models", "forecasts", "snapshots", "reports"]:
    files.extend(p for p in (source / directory).glob("*") if p.is_file())
manifest = []
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w") as archive:
    for file in sorted(files):
        data = file.read_bytes()
        name = str(file.relative_to(source))
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = 0
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(data))
        manifest.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
(out / "artifacts.tar.gz").write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
for name in ["backtest.json", "submission.csv"]:
    (out / name).write_bytes((source / name).read_bytes())
(out / "manifest.json").write_text(
    json.dumps(
        {"snapshot": json.loads((source / "current.json").read_bytes()), "files": manifest},
        ensure_ascii=False,
        indent=2,
    )
)
print(f"{len(files)} immutable files, {(out / 'artifacts.tar.gz').stat().st_size} archive bytes")
