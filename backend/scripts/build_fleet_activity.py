"""Estimate average observed activity in five-minute bins, without retaining identifiers.

One vehicle seen once in an hour contributes 1/12 vehicle-hour, not a whole
vehicle-hour. Short gaps (at most 20 minutes between bin starts) on the same
route are filled unless another route has evidence in the gap. The unfilled
proxy is retained separately. Neither series is a dispatch log.
"""

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROUTES = (1, 5, 7, 11, 12, 17, 25, 26, 28, 50)
START, END = "2025-01-01", "2025-11-01"
MAX_GAP_BINS = 4


def fill_short_gaps(mask, other_routes=0):
    result, previous = mask, None
    while mask:
        bit = mask & -mask
        position = bit.bit_length() - 1
        if previous is not None and position - previous <= MAX_GAP_BINS:
            gap = ((1 << position) - 1) ^ ((1 << (previous + 1)) - 1)
            if not gap & other_routes:
                result |= gap
        previous = position
        mask ^= bit
    return result


def scan(paths, chunk_size=500_000):
    masks, events, identified = defaultdict(int), Counter(), Counter()
    files, started = [], time.monotonic()
    for path in paths:
        rows = outside = absent = 0
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
            routes = chunk.ngpt_route.str.extract(r"^(\d+)\s+трамвай$", expand=False)
            if routes.isna().any() or not set(routes.astype(int)).issubset(ROUTES):
                raise ValueError("Unrecognized route")
            inside = timestamps.ge(START) & timestamps.lt(END)
            outside += int((~inside).sum())
            times = timestamps[inside]
            frame = pd.DataFrame(
                {
                    "route": routes[inside].astype(int),
                    "day": chunk.tran_date_time[inside].str[:10],
                    "hour": chunk.tran_date_time[inside].str[:13],
                    "bin": times.dt.hour * 12 + times.dt.minute // 5,
                    "vehicle": chunk.garage_number[inside].str.strip(),
                }
            )
            valid = frame.vehicle.str.fullmatch(r"[0-9]+") & frame.vehicle.str.contains(r"[1-9]")
            absent += int((~valid).sum())
            events.update(frame.groupby(["route", "hour"]).size().to_dict())
            known = frame[valid].copy()
            known["vehicle"] = known.vehicle.str.lstrip("0")
            identified.update(known.groupby(["route", "hour"]).size().to_dict())
            for route, day, vehicle, bucket in (
                known[["route", "day", "vehicle", "bin"]].drop_duplicates().itertuples(index=False, name=None)
            ):
                masks[route, day, vehicle] |= 1 << bucket
            if rows % 5_000_000 < chunk_size:
                print(f"{path.name}: {rows:,} rows; {time.monotonic() - started:.0f}s", flush=True)
        with path.open("rb") as f:
            checksum = hashlib.file_digest(f, "sha256").hexdigest()
        files.append(
            {
                "file": path.name,
                "sha256": checksum,
                "rows": rows,
                "outsideHistory": outside,
                "missingVehicleEvents": absent,
            }
        )
    hours = pd.date_range(START, END, freq="h", inclusive="left").strftime("%Y-%m-%d %H")
    buckets = defaultdict(lambda: [0] * 288)
    direct_buckets = defaultdict(lambda: [0] * 288)
    distinct = Counter()
    # Two routes can record the same vehicle around a route change. Retain that
    # evidence per route, but report simultaneous identifiers as a quality flag.
    union, overlaps = defaultdict(int), defaultdict(int)
    for (route, day, vehicle), mask in masks.items():
        key = day, vehicle
        overlaps[key] |= union[key] & mask
        union[key] |= mask
    for (route, day, vehicle), mask in masks.items():
        for hour in range(24):
            if mask & (4095 << (hour * 12)):
                distinct[route, f"{day} {hour:02d}"] += 1
        other_routes = (union[day, vehicle] ^ mask) | overlaps[day, vehicle]
        for target, active in ((direct_buckets, mask), (buckets, fill_short_gaps(mask, other_routes))):
            while active:
                bit = active & -active
                target[route, day][bit.bit_length() - 1] += 1
                active ^= bit
    data = {}
    for route in ROUTES:
        counts, direct, peaks, old, event_counts, known_counts = [], [], [], [], [], []
        for timestamp in hours:
            key = route, timestamp
            offset = int(timestamp[11:13]) * 12
            samples = buckets[route, timestamp[:10]][offset : offset + 12]
            counts.append(sum(samples) / 12)
            direct.append(sum(direct_buckets[route, timestamp[:10]][offset : offset + 12]) / 12)
            peaks.append(max(samples))
            old.append(distinct[key])
            event_counts.append(events[key])
            known_counts.append(identified[key])
        data[str(route)] = {
            "vehicles": counts,
            "directActivityVehicles": direct,
            "peakFiveMinuteVehicles": peaks,
            "hourlyDistinctVehicles": old,
            "events": event_counts,
            "identifiedEvents": known_counts,
        }
    return {
        "schemaVersion": 2,
        "start": f"{START}T00:00:00+03:00",
        "end": f"{END}T00:00:00+03:00",
        "binMinutes": 5,
        "maximumGapMinutes": 20,
        "method": "mean_garage_activity_5min_short_gaps_20min",
        "limitation": "Presence between nearby validations is assumed. Silent vehicles, longer gaps and midnight gaps remain unobserved. Not actual simultaneous fleet.",
        "crossRouteVehicleBins": sum(mask.bit_count() for mask in overlaps.values()),
        "inputs": files,
        "routes": data,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("dataset"))
    parser.add_argument("--output", type=Path, default=Path("dataset/derived/fleet/observed-activity.json"))
    args = parser.parse_args()
    result = scan([args.dataset_dir / "train.csv", args.dataset_dir / "test.csv"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(".pending.json")
    pending.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    pending.replace(args.output)
    print(f"Saved {args.output}", flush=True)
