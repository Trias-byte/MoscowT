"""Download the pinned public timetable sources; verify bytes before using them."""

import gzip
import hashlib
import io
import json
import urllib.request
from pathlib import Path

folder = Path(__file__).resolve().parents[2] / "dataset/derived/schedules-2025"
manifest = json.loads((folder / "sources.json").read_bytes())
for source in manifest["sources"]:
    if not source.get("localFile"):
        continue
    path = folder / source["localFile"]
    if path.exists():
        cached = path.read_bytes()
        if path.suffix == ".gz":
            cached = gzip.decompress(cached)
        if source.get("sha256") and hashlib.sha256(cached).hexdigest() != source["sha256"]:
            raise ValueError(f"Checksum mismatch: {path}")
        print(f"Cached: {source['id']}")
        continue
    with urllib.request.urlopen(source.get("downloadUrl", source["url"]), timeout=45) as response:
        data = response.read()
    if source.get("sha256") and hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError(f"Source changed: {source['id']}; inspect it before replacing the pinned source")
    if path.suffix == ".gz":
        # GzipFile pins the OS byte too, unlike gzip.compress across Python versions.
        buffer = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as stream:
            stream.write(data)
        data = buffer.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"Downloaded: {source['id']}")
