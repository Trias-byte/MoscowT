"""Stream the raw files once; retain route/hour counts, never card or validator IDs."""

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROUTES = (1, 5, 7, 11, 12, 17, 25, 26, 28, 50)
START, END = "2025-01-01", "2025-11-01"


def scan(paths, chunk_size=500_000):
    vehicles, events, identified = defaultdict(set), Counter(), Counter()
    files, started = [], time.monotonic()
    for path in paths:
        rows = outside = missing = 0
        for chunk in pd.read_csv(
            path,
            sep=";",
            usecols=["tran_date_time", "ngpt_route", "garage_number"],
            dtype=str,
            keep_default_na=False,
            chunksize=chunk_size,
        ):
            rows += len(chunk)
            timestamps = pd.to_datetime(chunk.tran_date_time, format="%Y-%m-%d %H:%M:%S")
            route = chunk.ngpt_route.str.extract(r"^(\d+)\s+трамвай$", expand=False)
            if route.isna().any() or not set(route.astype(int)).issubset(ROUTES):
                raise ValueError("Unrecognized route in raw observations")
            inside = timestamps.ge(START) & timestamps.lt(END)
            outside += int((~inside).sum())
            frame = pd.DataFrame(
                {
                    "route": route[inside].astype(int),
                    "hour": chunk.tran_date_time[inside].str[:13],
                    "vehicle": chunk.garage_number[inside].str.strip(),
                }
            )
            valid = frame.vehicle.str.fullmatch(r"[0-9]+") & frame.vehicle.str.contains(r"[1-9]")
            missing += int((~valid).sum())
            events.update(frame.groupby(["route", "hour"]).size().to_dict())
            known = frame[valid].copy()
            # Treat alternate zero padding of a garage number as one vehicle.
            known["vehicle"] = known.vehicle.str.lstrip("0")
            identified.update(known.groupby(["route", "hour"]).size().to_dict())
            for route_id, hour, vehicle in known.drop_duplicates().itertuples(index=False, name=None):
                vehicles[(route_id, hour)].add(vehicle)
            if rows % 5_000_000 < chunk_size:
                print(f"{path.name}: {rows:,} rows; {time.monotonic() - started:.0f}s", flush=True)
        with path.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        files.append(
            {
                "file": path.name,
                "sha256": checksum,
                "rows": rows,
                "outsideHistory": outside,
                "missingVehicleEvents": missing,
            }
        )
        print(f"Finished {path.name}: {rows:,} rows", flush=True)
    hours = pd.date_range(START, END, freq="h", inclusive="left").strftime("%Y-%m-%d %H")
    data = {}
    for route in ROUTES:
        keys = [(route, hour) for hour in hours]
        data[str(route)] = {
            "vehicles": [len(vehicles[key]) for key in keys],
            "events": [events[key] for key in keys],
            "identifiedEvents": [identified[key] for key in keys],
        }
    return {
        "schemaVersion": 1,
        "start": f"{START}T00:00:00+03:00",
        "end": f"{END}T00:00:00+03:00",
        "method": "distinct_garage_number_per_route_hour_all_validation_results",
        "inputs": files,
        "routes": data,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("dataset"))
    parser.add_argument("--output", type=Path, default=Path("dataset/derived/fleet/observed-fleet.json"))
    args = parser.parse_args()
    result = scan([args.dataset_dir / "train.csv", args.dataset_dir / "test.csv"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(".pending.json")
    pending.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    pending.replace(args.output)
    print(f"Saved {args.output}", flush=True)
