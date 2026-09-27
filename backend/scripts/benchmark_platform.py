"""Separate sequential latency measurements for v2 day/scenario/year/spatial reads."""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import aiohttp


async def main(args):
    report = {
        "url": args.url,
        "samples_per_case": args.samples,
        "concurrency": 1,
        "cases": {},
        "created_at": time.time(),
    }
    async with aiohttp.ClientSession(args.url, timeout=aiohttp.ClientTimeout(total=120)) as client:

        async def get(path):
            async with client.get(path) as response:
                response.raise_for_status()
                return await response.json()

        async def post(path, body):
            async with client.post(path, json=body) as response:
                content = await response.read()
                if response.status != 200:
                    raise RuntimeError(content.decode())
                return content

        cap = await get("/api/v2/capabilities")
        current = cap["current_snapshot"]
        run = await get("/api/v2/forecast-runs/" + current["forecastId"])
        runs = await get("/api/v2/forecast-runs")
        annual = next(
            r
            for r in runs
            if r["model_type"] == "annual_scenario" and r["spec"]["dataset_id"] == cap["current_dataset_id"]
        )
        routes = run["spec"]["route_ids"]
        period = {"start": "2025-11-01T00:00:00+03:00", "end": "2025-11-02T00:00:00+03:00"}
        query = {
            "dataset_id": cap["current_dataset_id"],
            "forecast_id": run["id"],
            "route_ids": routes,
            "time_range": period,
            "mode": "forecast",
        }
        scenario = json.loads(
            await post(
                "/api/v2/scenarios",
                {
                    "forecast_id": run["id"],
                    "route_ids": routes,
                    "time_range": period,
                    "coefficients": {"weather": 1.1, "event": 1.05},
                    "additional_vehicle_hours": 12,
                },
            )
        )
        cases = {
            "day": ("/api/v2/forecasts/query", query),
            "scenario_day": ("/api/v2/forecasts/query", {**query, "scenario_id": scenario["id"]}),
            "year_monthly": (
                "/api/v2/forecasts/query",
                {
                    **query,
                    "forecast_id": annual["id"],
                    "time_range": annual["spec"]["time_range"],
                    "grain": "month",
                },
            ),
            "sections_day": (
                "/api/v2/sections",
                {"snapshotId": current["snapshotId"], "routeIds": routes, "timeRange": period},
            ),
        }
        report.update(snapshot_id=current["snapshotId"], dataset_id=cap["current_dataset_id"])
        for name, (path, body) in cases.items():
            samples = []
            sizes = []
            errors = []
            for _ in range(args.samples):
                start = time.perf_counter()
                try:
                    sizes.append(len(await post(path, body)))
                except Exception as exc:
                    errors.append(str(exc))
                samples.append((time.perf_counter() - start) * 1000)
            values = sorted(samples[1:])
            report["cases"][name] = {
                "path": path,
                "first_ms": samples[0],
                "repeated_p50_ms": values[len(values) // 2],
                "repeated_p95_ms": values[math.ceil(len(values) * 0.95) - 1],
                "max_ms": max(samples),
                "mean_response_bytes": sum(sizes) / len(sizes) if sizes else None,
                "errors": errors,
                "latencies_ms": samples,
                "spec": body,
            }
            print(
                name, report["cases"][name]["first_ms"], report["cases"][name]["repeated_p95_ms"], flush=True
            )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", default="docs/performance-v2-extra.json")
    asyncio.run(main(parser.parse_args()))
