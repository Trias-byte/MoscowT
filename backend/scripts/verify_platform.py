"""End-to-end acceptance against an isolated Docker project with real source labels.

Temporarily selects older dataset versions and restores the original publication.
Do not target a shared operator session.
"""

import argparse
import io
import json
import math
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd


def main(args):
    evidence = {"base_url": args.url, "started_at": pd.Timestamp.now(tz="UTC").isoformat(), "checks": []}

    def request(path, body=None, raw=None):
        content = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = Request(
            args.url + path,
            data=content,
            headers={
                "Content-Type": "application/octet-stream" if raw is not None else "application/json",
                "Idempotency-Key": uuid.uuid4().hex,
            },
        )
        with urlopen(req, timeout=120) as response:
            payload = response.read()
            return payload if path.endswith("/download") else json.loads(payload)

    def job(path, body):
        result = request(path, body)
        deadline = time.monotonic() + 180
        while result["status"] in ("pending", "running"):
            if time.monotonic() > deadline:
                raise TimeoutError(result)
            time.sleep(0.3)
            result = request("/api/v2/jobs/" + result["id"])
        assert result["status"] == "ready", result
        return result

    def check(name, **values):
        evidence["checks"].append({"name": name, **values})
        print(name, flush=True)

    def period(start, end):
        return {"start": start + "T00:00:00+03:00", "end": end + "T00:00:00+03:00"}

    def ingest(frame, routes, start, end):
        blob = request("/api/v2/blobs", raw=frame.to_csv(index=False, sep=";").encode())
        preview = job(
            "/api/v2/uploads",
            {
                "blob_id": blob["id"],
                "spec": {
                    "kind": "labels",
                    "route_ids": routes,
                    "time_range": period(start, end),
                    "complete": True,
                },
            },
        )["result"]
        return job("/api/v2/uploads/" + preview["id"] + "/apply", {})["result"]

    cap = request("/api/v2/capabilities")
    original = cap["current_snapshot"]
    versions = request("/api/v2/datasets")["versions"]
    baseline = next(v for v in versions if len(v["routes"]) == 9 and v["end"].startswith("2025-09-01"))
    routes9 = baseline["routes"]
    source = Path(args.source_dir) / "labels"
    labels = pd.concat(
        [
            pd.read_csv(source / ("labels_day_" + part + ".csv"), sep=";", dtype={"route": str})
            for part in ("train", "test")
        ]
    )
    try:
        request("/api/v2/datasets/" + baseline["id"] + "/activate", {})
        september = labels.loc[
            labels.route.isin(routes9) & labels.date.ge("2025-09-01") & labels.date.lt("2025-10-01")
        ]
        imported = ingest(september, routes9, "2025-09-01", "2025-10-01")
        repeated = ingest(september, routes9, "2025-09-01", "2025-10-01")
        assert repeated["duplicate"] and repeated["dataset_id"] == imported["dataset_id"]
        check("september_and_duplicate", dataset_id=imported["dataset_id"], duplicate=True)
        training = {
            "dataset_id": imported["dataset_id"],
            "model_type": "seasonal",
            "route_ids": routes9,
            "time_range": period("2025-01-01", "2025-10-01"),
        }
        seasonal = job("/api/v2/training-runs", training)["result"]
        newroute = labels.loc[labels.route.eq("17") & labels.date.lt("2025-10-01")]
        imported = ingest(newroute, ["17"], "2025-01-01", "2025-10-01")
        routes = sorted([*routes9, "17"])
        training.update(
            dataset_id=imported["dataset_id"],
            route_ids=routes,
            model_type="lgb_cb_rf",
            parameters={"lgb_estimators": 16, "cat_iterations": 24, "rf_estimators": 16},
        )
        ensemble = job("/api/v2/training-runs", training)["result"]
        check("new_route_and_two_adapters", route="17", models=[seasonal["id"], ensemble["id"]])
        spec = {
            "model_id": ensemble["id"],
            "dataset_id": imported["dataset_id"],
            "route_ids": routes,
            "origin": "2025-10-01T00:00:00+03:00",
        }
        day = job("/api/v2/forecast-runs", {**spec, "time_range": period("2025-10-01", "2025-10-02")})[
            "result"
        ]
        month = job("/api/v2/forecast-runs", {**spec, "time_range": period("2025-10-01", "2025-11-01")})[
            "result"
        ]
        annual = job(
            "/api/v2/training-runs", {**training, "model_type": "annual_scenario", "parameters": {}}
        )["result"]
        year = job(
            "/api/v2/forecast-runs",
            {**spec, "model_id": annual["id"], "time_range": period("2025-10-01", "2026-10-01")},
        )["result"]
        yearly = request(
            "/api/v2/forecasts/query",
            {
                "dataset_id": imported["dataset_id"],
                "forecast_id": year["id"],
                "route_ids": routes,
                "time_range": year["spec"]["time_range"],
                "grain": "month",
                "mode": "forecast",
            },
        )
        assert len(yearly["rows"]) == 120 and "Невалидированный" in year["quality_note"]
        check("day_month_year", runs=[day["id"], month["id"], year["id"]], year_month_rows=120)
        schedule = request("/api/v2/schedules")[0]
        published = request(
            "/api/v2/publications",
            {"forecast_id": month["id"], "schedule_id": schedule["id"], "schedule_scenario": True},
        )
        selected = {
            "forecast_id": month["id"],
            "route_ids": ["17"],
            "time_range": period("2025-10-01", "2025-10-02"),
        }
        query = {
            **selected,
            "dataset_id": imported["dataset_id"],
            "mode": "forecast",
            "schedule_id": schedule["id"],
            "schedule_scenario": True,
        }
        base = request("/api/v2/forecasts/query", query)["rows"]
        scenario = request(
            "/api/v2/scenarios", {**selected, "coefficients": {"event": 1.2}, "additional_vehicle_hours": 6}
        )
        adjusted = request("/api/v2/forecasts/query", {**query, "scenario_id": scenario["id"]})["rows"]
        assert all(math.isclose(a["value"], b["value"] * 1.2) for a, b in zip(adjusted, base, strict=True))
        export = job("/api/v2/exports", {**query, "scenario_id": scenario["id"]})
        csv = pd.read_csv(io.BytesIO(request("/api/v2/jobs/" + export["id"] + "/download")), sep=";")
        assert all(math.isclose(v, r["value"]) for v, r in zip(csv.value, adjusted, strict=True))
        scope = {
            "snapshotId": published["snapshotId"],
            "routeIds": ["17"],
            "timeRange": selected["time_range"],
            "scenarioId": scenario["id"],
            "mode": "forecast",
        }
        mapview = request("/api/v1/map-snapshot", scope)
        assert math.isclose(sum(f["values"][0]["value"] for f in mapview["frames"]), csv.value.sum())
        stops = request(
            "/api/v2/forecasts/query",
            {**query, "metric_scope": "stop", "snapshot_id": published["snapshotId"]},
        )["rows"]
        assert math.isclose(sum(r["value"] for r in stops), sum(r["value"] for r in base))
        sections = request("/api/v2/sections", scope)["sections"]
        assert sections
        check(
            "scenario_map_csv_and_stop_conservation",
            scenario_id=scenario["id"],
            base=sum(r["value"] for r in base),
            adjusted=float(csv.value.sum()),
            sections=len(sections),
            vehicle_hours=float(csv.vehicle_hours.sum()),
        )
        for name, coefficients, hours in [
            ("peak_reinforcement", {}, 2),
            ("event_demand", {"event": 1.2}, 0),
        ]:
            example_period = (
                {"start": "2025-10-01T08:00:00+03:00", "end": "2025-10-01T09:00:00+03:00"}
                if name == "peak_reinforcement"
                else selected["time_range"]
            )
            scenario = request(
                "/api/v2/scenarios",
                {
                    **selected,
                    "time_range": example_period,
                    "coefficients": coefficients,
                    "additional_vehicle_hours": hours,
                },
            )
            result = request(
                "/api/v2/forecasts/query",
                {**query, "time_range": example_period, "scenario_id": scenario["id"]},
            )["rows"]
            validations = sum(r["value"] for r in result)
            vehicle_hours = sum(r["vehicle_hours"] for r in result if r["vehicle_hours"] is not None)
            check(
                name,
                scenario_id=scenario["id"],
                validations=validations,
                vehicle_hours=vehicle_hours,
                intensity=validations / vehicle_hours if vehicle_hours else None,
            )
        timetable = []
        for extra in (False, True):
            rows = ["route;service_date;grafic;trip_num;direction;start;end;garage_number"]
            rows += [f"17;2025-10-01;{i};1;planned;05:00;23:00;{i}" for i in (101, 102, 103)]
            if extra:
                rows.append("17;2025-10-01;104;1;planned;08:00;09:00;104")
            blob = request("/api/v2/blobs", raw=("\n".join(rows) + "\n").encode())
            duty = job("/api/v2/schedules", {"blob_id": blob["id"]})["result"]
            result = request(
                "/api/v2/forecasts/query",
                {
                    **query,
                    "time_range": {"start": "2025-10-01T08:00:00+03:00", "end": "2025-10-01T09:00:00+03:00"},
                    "schedule_id": duty["id"],
                    "schedule_scenario": False,
                },
            )["rows"][0]
            timetable.append(
                {
                    "schedule_id": duty["id"],
                    "vehicle_hours": result["vehicle_hours"],
                    "intensity": result["validations_per_vehicle_hour"],
                }
            )
        assert timetable[0]["vehicle_hours"] == 3 and timetable[1]["vehicle_hours"] == 4
        check(
            "timetable_change",
            kind="Hypothetical planned duties, not observed operator release",
            before=timetable[0],
            after=timetable[1],
        )
        parquet = job("/api/v2/exports", {**query, "format": "parquet"})
        import zipfile

        with zipfile.ZipFile(io.BytesIO(request("/api/v2/jobs/" + parquet["id"] + "/download"))) as archive:
            assert set(archive.namelist()) == {"route_hours.parquet", "manifest.json"}
        check("parquet_with_manifest", job_id=parquet["id"])
        check("workers_healthy", model_jobs="completed through read-only data mount")
    finally:
        request("/api/v2/datasets/" + cap["current_dataset_id"] + "/activate", {})
        request(
            "/api/v2/publications",
            {
                "forecast_id": original["forecastId"],
                "schedule_id": original.get("scheduleId"),
                "schedule_scenario": original.get("scheduleScenario", False),
            },
        )
    evidence["finished_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output", default="docs/platform-acceptance.json")
    main(parser.parse_args())
