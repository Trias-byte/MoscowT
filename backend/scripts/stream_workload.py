"""Measured HTTP microbatch ingestion into a separate, unpublished dataset."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta


async def ingest(client, seconds, rows_per_batch=10000):
    started = time.perf_counter()
    report = {
        "transport": "chunked HTTP CSV + queued preview/apply",
        "synthetic": True,
        "kind": "validation_events",
        "recordsPerBatch": rows_per_batch,
        "batches": [],
        "errors": [],
    }
    dataset_id = None
    name = "Нагрузочная проверка HTTP микропакетов (синтетика) " + uuid.uuid4().hex[:8]
    routes = [f"load-{i}" for i in range(10)]

    async def request(path, body=None):
        async with client.request(
            "GET" if body is None else "POST", path, json=body, headers={"Idempotency-Key": uuid.uuid4().hex}
        ) as response:
            payload = await response.json()
            if response.status not in (200, 202):
                raise RuntimeError(f"{response.status}: {payload}")
            return payload

    async def job(path, body):
        value = await request(path, body)
        deadline = time.perf_counter() + 60
        while value["status"] in ("pending", "running"):
            if time.perf_counter() > deadline:
                raise TimeoutError(value["id"])
            await asyncio.sleep(0.1)
            value = await request("/api/v2/jobs/" + value["id"])
        if value["status"] != "ready":
            raise RuntimeError(json.dumps(value))
        return value["result"]

    original = await request("/api/v2/capabilities")
    try:
        while time.perf_counter() - started < seconds:
            index = len(report["batches"])
            day = datetime(2025, 1, 1) + timedelta(hours=index)
            rows = [
                f"{routes[i % 10]};{day + timedelta(seconds=(i // 10) % 3600):%d.%m.%Y %H:%M:%S};{2 if i % 13 == 0 else 1};{i % 10};{100 + i % 10}"
                for i in range(rows_per_batch)
            ]
            content = (
                "ngpt_route;tran_date_time;validation_result;bus_exit_no;garage_number\n"
                + "\n".join(rows)
                + "\n"
            ).encode()

            async def chunks():
                for offset in range(0, len(content), 16384):
                    yield content[offset : offset + 16384]
                    await asyncio.sleep(0)

            begin = time.perf_counter()
            async with client.post(
                "/api/v2/blobs", data=chunks(), headers={"Content-Type": "text/csv"}
            ) as response:
                blob = await response.json()
                if response.status != 201:
                    raise RuntimeError(str(blob))
            upload_ms = (time.perf_counter() - begin) * 1000
            preview = await job(
                "/api/v2/uploads",
                {
                    "blob_id": blob["id"],
                    "spec": {
                        "kind": "events",
                        "mode": "append",
                        "route_ids": routes,
                        "dataset_name": name,
                        "new_dataset": dataset_id is None,
                        "base_dataset_id": dataset_id,
                        "time_range": {
                            "start": day.isoformat() + "+03:00",
                            "end": (day + timedelta(hours=1)).isoformat() + "+03:00",
                        },
                    },
                },
            )
            applied = await job("/api/v2/uploads/" + preview["id"] + "/apply", {})
            if applied["duplicate"]:
                raise RuntimeError("This benchmark must measure fresh commits, not duplicate imports")
            dataset_id = applied["dataset_id"]
            report["batches"].append(
                {
                    "records": len(rows),
                    "selectedRecords": preview["selected_rows"],
                    "bytes": len(content),
                    "uploadMs": upload_ms,
                    "commitMs": (time.perf_counter() - begin) * 1000,
                    "datasetId": dataset_id,
                }
            )
        report["elapsedSeconds"] = time.perf_counter() - started
        manifest = await request("/api/v2/datasets/" + dataset_id) if dataset_id else {}
        report["committedRouteHours"] = manifest.get("rows", 0)
        report["committedRecords"] = sum(batch["selectedRecords"] for batch in report["batches"])
        report["recordsPerSecond"] = report["committedRecords"] / report["elapsedSeconds"]
        report["sumVerified"] = manifest.get("total") == len(report["batches"]) * sum(
            i % 13 != 0 for i in range(rows_per_batch)
        )
        report["rowsVerified"] = (
            manifest.get("known_rows") == report["committedRouteHours"] == len(report["batches"]) * 10
        )
        after = await request("/api/v2/capabilities")
        report["publicationUnchanged"] = original["current_snapshot"] == after["current_snapshot"]
        report["defaultDatasetUnchanged"] = original["current_dataset_id"] == after["current_dataset_id"]
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    return report
