"""Open-loop HTTP workload. Request latency includes generator scheduling delay."""

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp


async def main(args):
    samples = []
    errors = Counter()
    sizes = []
    resource = []
    export_jobs = []
    async with aiohttp.ClientSession(
        base_url=args.url,
        timeout=aiohttp.ClientTimeout(total=15),
        connector=aiohttp.TCPConnector(limit=160),
    ) as client:

        async def fetch(path, body=None, headers=None):
            async with client.request(
                "GET" if body is None else "POST", path, json=body, headers=headers
            ) as r:
                return r.status, await r.read()

        cap = json.loads((await fetch("/api/v1/capabilities"))[1])
        routes = cap["targetRouteIds"]
        snapshot = cap["snapshotId"]
        variants = []
        for i in range(24 if args.mode == "warm" else 600):
            origin = (
                (datetime(2025, 11, 1) + timedelta(days=i % 60))
                if args.mode == "warm"
                else datetime(2025, 1, 1) + timedelta(days=i % 303, hours=(i // 303) * 12)
            )
            route_ids = routes if i % 3 == 0 else [routes[i % 10], routes[(i + 3) % 10], routes[(i + 6) % 10]]
            body = {
                "routeIds": route_ids,
                "snapshotId": snapshot,
                "timeRange": {
                    "start": origin.strftime("%Y-%m-%dT%H:00:00+03:00"),
                    "end": (origin + timedelta(days=1)).strftime("%Y-%m-%dT%H:00:00+03:00"),
                },
            }
            if args.mode == "cold":
                body["mode"] = "history"
            if i % 5 == 0 and args.mode == "warm":
                body["grain"] = "day"
                body["timeRange"]["end"] = (origin + timedelta(days=min(7, 61 - i % 60))).strftime(
                    "%Y-%m-%dT%H:00:00+03:00"
                )
            variants.append(body)
        if args.mode == "warm":
            for body in variants:
                await fetch("/api/v1/map-snapshot", body)

        async def stats():
            while True:
                if args.container:
                    p = await asyncio.create_subprocess_exec(
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{json .}}",
                        args.container,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    out, _ = await p.communicate()
                    if out.strip():
                        resource.append(json.loads(out))
                await asyncio.sleep(1)

        monitor = asyncio.create_task(stats())
        start = time.perf_counter()

        async def request(i, scheduled):
            path = [
                "map-snapshot",
                "timeseries",
                "route-comparison",
                "heatmap",
                "map-snapshot",
                "timeseries",
                "heatmap",
                "route-comparison",
                "capabilities",
                "routes",
            ][i % 10]
            try:
                status, content = await fetch(
                    "/api/v1/" + path,
                    None if path in ["capabilities", "routes"] else variants[i % len(variants)],
                )
                samples.append((time.perf_counter() - scheduled) * 1000)
                sizes.append(len(content))
                if status != 200:
                    errors[str(status)] += 1
            except Exception as exc:
                errors[type(exc).__name__] += 1

        async def exports():
            for i in range(max(1, args.seconds // 5)):
                await asyncio.sleep(1 if i == 0 else 5)
                _, content = await fetch(
                    "/api/v1/exports",
                    {"scope": variants[i % len(variants)], "index": None},
                    headers={"Idempotency-Key": f"load-{args.mode}-{start}-{i}"},
                )
                export_jobs.append({"submittedAt": time.perf_counter(), **json.loads(content)})

        exporting = asyncio.create_task(exports()) if args.exports else None
        # Bound active client coroutines independently of the offered schedule.
        # The generator's queue must not become a growing HTTP connection-pool queue.
        pending = asyncio.Queue()

        async def consume():
            while True:
                item = await pending.get()
                try:
                    if item is None:
                        return
                    await request(*item)
                finally:
                    pending.task_done()

        consumers = [asyncio.create_task(consume()) for _ in range(96)]
        for i in range(args.seconds * args.rps):
            scheduled = start + i / args.rps
            await asyncio.sleep(max(0, scheduled - time.perf_counter()))
            pending.put_nowait((i, scheduled))
        await pending.join()
        for _ in consumers:
            pending.put_nowait(None)
        await asyncio.gather(*consumers)
        elapsed = time.perf_counter() - start
        if exporting:
            await exporting
        for job in export_jobs:
            if "id" in job:
                final = json.loads((await fetch("/api/v1/jobs/" + job["id"]))[1])
                job.update(
                    status=final["status"], checkedAfterSeconds=time.perf_counter() - job.pop("submittedAt")
                )
        metrics = (await fetch("/metrics"))[1].decode()
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
    values = sorted(samples)

    def percentile(p):
        return values[min(len(values) - 1, math.ceil(len(values) * p) - 1)] if values else None

    report = {
        "mode": args.mode,
        "offeredRps": args.rps,
        "requestedDurationSeconds": args.seconds,
        "elapsedSeconds": elapsed,
        "completed": len(samples),
        "achievedRps": len(samples) / elapsed,
        "errors": dict(errors),
        "p50Ms": percentile(0.50),
        "p95Ms": percentile(0.95),
        "p99Ms": percentile(0.99),
        "responseBytes": {"min": min(sizes), "mean": statistics.mean(sizes), "max": max(sizes)},
        "mix": {
            "map-snapshot": 0.2,
            "timeseries": 0.2,
            "route-comparison": 0.2,
            "heatmap": 0.2,
            "capabilities": 0.1,
            "routes": 0.1,
        },
        "scopeVariants": len(variants),
        "resources": resource,
        "exportJobs": export_jobs,
        "metrics": metrics,
        "latencyIncludesSchedulingDelay": True,
        "clientConcurrency": 96,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ["metrics", "resources", "exportJobs"]}, indent=2
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--container")
    parser.add_argument("--mode", choices=["warm", "cold"], default="warm")
    parser.add_argument("--rps", type=int, default=300)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--exports", action="store_true")
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
