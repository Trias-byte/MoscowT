"""Download frozen sources and write the reproducible 2025 incident delivery."""

import argparse
import shutil
from pathlib import Path

from moscowt.config import Settings
from moscowt.factors import FactorRepository
from moscowt.storage import SnapshotStore, file_hash, write_json

parser = argparse.ArgumentParser()
parser.add_argument("--kind", choices=["weather", "accidents"], required=True)
parser.add_argument("--archive", type=Path, help="Use a frozen original accident ZIP without downloading")
parser.add_argument("--output", type=Path, default=Path("dataset/derived/moscow_accidents_2025"))
args = parser.parse_args()
settings = Settings()
repository = FactorRepository(settings.data_root)
if args.kind == "weather":
    print(repository.fetch_weather(), flush=True)
else:
    manifest = (
        repository.import_accidents(args.archive.read_bytes())
        if args.archive
        else repository.fetch_accidents()
    )
    print("Downloaded", manifest["rows"], "incidents", flush=True)
    store = SnapshotStore(settings.state_dir)
    links = repository.link_accidents(manifest["id"], store, store.current()["networkId"])
    args.output.mkdir(parents=True, exist_ok=True)
    for extension, name in [
        ("raw", "source.geojson.zip"),
        ("csv", "moscow_accidents_2025.csv"),
        ("parquet", "moscow_accidents_2025.parquet"),
    ]:
        shutil.copyfile(repository.store.path("accidents", manifest["id"], extension), args.output / name)
    shutil.copyfile(
        repository.store.path("accident_links", links["id"], "parquet"),
        args.output / "route_candidates.parquet",
    )
    write_json(
        args.output / "manifest.json",
        {
            "archive": manifest,
            "route_candidates": links,
            "files": {
                p.name: {"sha256": file_hash(p), "bytes": p.stat().st_size}
                for p in args.output.iterdir()
                if p.suffix in (".zip", ".csv", ".parquet")
            },
        },
    )
    print("Linked", links["rows"], "route candidates", flush=True)
