"""Verify the scenario/model HTTP workflow against a ready demo (no publication changes)."""

import argparse
import csv
import io
import json
import math
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


def main(args):
    report = {"url": args.url, "checks": {}, "created_at": time.time()}

    def request(path, body=None, raw=None, download=False):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = Request(
            args.url + "/api/v2" + path,
            data=data,
            headers={
                "Content-Type": "application/octet-stream" if raw is not None else "application/json",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        with urlopen(req, timeout=120) as response:
            content = response.read()
        return content if download else json.loads(content)

    def job(path, body):
        result = request(path, body)
        started = time.monotonic()
        while result["status"] in ("pending", "running"):
            if time.monotonic() - started > 180:
                raise TimeoutError(result)
            time.sleep(0.2)
            result = request("/jobs/" + result["id"])
        assert result["status"] == "ready", result
        return result

    cap = request("/capabilities")
    original_snapshot = cap["current_snapshot"]
    run = request("/forecast-runs/" + original_snapshot["forecastId"])
    period = {"start": "2025-11-01T08:00:00+03:00", "end": "2025-11-01T10:00:00+03:00"}
    spec = {"engine": "recompute", "forecast_id": run["id"], "route_ids": ["17"], "time_range": period}
    query = {
        "dataset_id": run["spec"]["dataset_id"],
        "forecast_id": run["id"],
        "route_ids": ["17"],
        "time_range": period,
        "mode": "forecast",
    }
    base = request("/forecasts/query", query)["rows"]
    for name, changes in {
        "neutral": {},
        "weather": {"weather": {"temperature_2m": -15, "relative_humidity_2m": 90, "precipitation": 5}},
        "service": {"schedule": {"base_headway_minutes": 10, "headway_minutes": 8, "elasticity": 0.3}},
        "full_closure": {
            "incidents": [
                {
                    "id": "controlled",
                    "longitude": 37.62,
                    "latitude": 55.75,
                    "route_ids": ["17"],
                    "start": period["start"],
                    "duration_minutes": 120,
                    "reduction": 1,
                }
            ]
        },
    }.items():
        draft = request("/scenarios", {**spec, **changes, "name": "Приёмка: " + name})
        started = time.monotonic()
        result = job("/scenarios/" + draft["id"] + "/runs", {})["result"]
        rows = request("/forecasts/query", {**query, "scenario_id": result["id"]})["rows"]
        if name == "neutral":
            assert [r["value"] for r in rows] == [r["value"] for r in base]
        if name == "weather":
            assert [r["value"] for r in rows] != [r["value"] for r in base]
        if name == "service":
            assert math.isclose(result["total"] / result["base_total"], 1.25**0.3)
        if name == "full_closure":
            assert all(r["value"] == 0 for r in rows)
        exported = job("/exports", {**query, "scenario_id": result["id"]})
        records = list(
            csv.DictReader(
                io.StringIO(
                    request("/jobs/" + exported["id"] + "/download", download=True).decode("utf-8-sig")
                ),
                delimiter=";",
            )
        )
        assert [float(r["value"]) for r in records] == [r["value"] for r in rows]
        report["checks"][name] = {
            "id": result["id"],
            "base": result["base_total"],
            "result": result["total"],
            "elapsed_seconds": time.monotonic() - started,
            "csv_matches": True,
        }
    profile = request("/forecast-runs/" + run["id"] + "/weather-profile", period)
    assert set(profile["fields"]) == {"temperature_2m", "relative_humidity_2m", "precipitation"}
    report["checks"]["weather_profile"] = profile
    exported = job("/models/" + run["spec"]["model_id"] + "/export", {})
    blob = request("/blobs?kind=model", raw=request("/jobs/" + exported["id"] + "/download", download=True))
    preview = request("/models/import-preview", {"blob_id": blob["id"]})
    assert preview["format"] == "potok-model-v1"
    imported = job("/models/import", {"blob_id": blob["id"]})["result"]
    forecast = job(
        "/forecast-runs",
        {**run["spec"], "model_id": imported["id"], "time_range": period, "route_ids": ["17"]},
    )["result"]
    replay = request("/forecasts/query", {**query, "forecast_id": forecast["id"]})["rows"]
    assert all(math.isclose(x["value"], y["value"], rel_tol=1e-10) for x, y in zip(base, replay, strict=True))
    revised = job(
        "/models/" + run["spec"]["model_id"] + "/weights?forecast_id=" + run["id"],
        {"catboost": 0.6, "lightgbm": 0.2, "random_forest": 0.2},
    )["result"]
    assert revised["forecast_id"] != run["id"]
    report["checks"]["model_transfer"] = {
        "imported_model": imported["id"],
        "roundtrip_matches": True,
        "reweighted_model": revised["id"],
        "recalculated_forecast": revised["forecast_id"],
    }
    assert request("/capabilities")["current_snapshot"] == original_snapshot
    report["snapshot_unchanged"] = True
    report["snapshot"] = original_snapshot
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["checks"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", default="docs/scenario-acceptance.json")
    main(parser.parse_args())
