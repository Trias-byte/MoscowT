from copy import deepcopy

from moscowt.analytics import segment_intersects_bbox


def test_readiness_requires_artifacts(tmp_path):
    from fastapi.testclient import TestClient

    from moscowt.api import create_app
    from moscowt.config import Settings
    from moscowt.storage import SnapshotStore

    store = SnapshotStore(tmp_path)
    with TestClient(create_app(Settings(state_dir=tmp_path, worker_enabled=False))) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        store.publish(historyId="history-missing", networkId="network-missing", forecastId="forecast-missing")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "histories/history-missing.parquet" in response.json()["missing"]


def test_capabilities_and_all_routes(client):
    c = client.get("/api/v1/capabilities").json()
    assert c["contractVersion"] == 2 and c["stream"] is False
    assert c["metrics"] == ["successful_validations"]
    routes = client.get("/api/v1/routes").json()
    targets = [r for r in routes if r["isTarget"]]
    assert len(targets) == 10
    assert {r["id"] for r in targets if not r["hasGeometry"]} == {"5"}
    assert client.get("/health/ready").status_code == 200
    assert "moscowt_http" in client.get("/metrics").text


def test_consistent_views_and_no_duplicate_sums(client, scope):
    data = [
        client.post("/api/v1/" + p, json=scope).json()
        for p in ("map-snapshot", "timeseries", "route-comparison", "heatmap")
    ]
    assert len({d["meta"]["snapshotId"] for d in data}) == 1
    assert len({d["meta"]["networkSnapshotId"] for d in data}) == 1
    assert len(data[0]["frames"]) == 24
    for frame in data[0]["frames"]:
        assert len(frame["values"]) == 10
        assert frame["aggregate"] == sum(v["value"] for v in frame["values"])
    assert len(data[3]["cells"]) == 240
    assert data[3]["routeIds"] == scope["routeIds"]


def test_section_does_not_change_numbers(client, scope):
    all_values = client.post("/api/v1/map-snapshot", json=scope).json()
    network = client.get("/api/v1/network?date=2025-11-01").json()
    p = next(p for p in network["patterns"] if p["routeId"] == "1")
    visits = [s for s in network["stops"] if s["patternId"] == p["id"]]
    filtered = deepcopy(scope)
    filtered["geometry"] = {
        "patternIds": [p["id"]],
        "section": {"patternId": p["id"], "fromId": visits[1]["id"], "toId": visits[4]["id"]},
    }
    r = client.post("/api/v1/map-snapshot", json=filtered).json()
    assert r["frames"] == all_values["frames"]
    assert len(r["geometry"]["segmentIds"]) == 3
    assert r["meta"]["geometryFilterAffectsMetric"] is False
    assert r["geometry"]["missingGeometryRouteIds"] == ["5"]


def test_historical_geography_resolves_january_2025(client, scope):
    scope.update(
        mode="history",
        timeRange={"start": "2025-01-01T00:00:00+03:00", "end": "2025-01-02T00:00:00+03:00"},
        geometry={"referenceMode": "historical"},
    )
    r = client.post("/api/v1/map-snapshot", json=scope).json()
    assert r["geometry"]["segmentIds"]
    assert not r["geometry"]["historicallyUnavailablePatternIds"]
    assert r["geometry"]["asOf"] == "2025-01-01"
    assert r["frames"][0]["aggregate"] is not None


def test_unsupported_grain_and_metric_are_explicit(client, scope):
    r = client.post("/api/v1/route-profile", json=scope)
    assert r.status_code == 422 and r.json()["error"]["code"] == "METRIC_GRAIN_UNAVAILABLE"
    r = client.post("/api/v1/timeseries", json={**scope, "metricScope": "stop"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "METRIC_GRAIN_UNAVAILABLE"
    r = client.post("/api/v1/timeseries", json={**scope, "metric": "occupancy"})
    assert r.status_code == 422


def test_invalid_inputs(client, scope):
    for updates in [
        {"routeIds": ["13"]},
        {"routeIds": ["1", "1"]},
        {"snapshotId": "../current"},
        {"timeRange": {"start": "2025-11-01T00:00:00", "end": "2025-11-02T00:00:00"}},
        {"geometry": {"bbox": [10, 20, 5, 30]}},
    ]:
        r = client.post("/api/v1/map-snapshot", json={**scope, **updates})
        assert r.status_code in (404, 422)
    r = client.post("/api/v1/timeseries", json={**scope, "snapshotId": "snapshot-missing"})
    assert r.status_code == 404


def test_crossing_segment_without_inner_vertices():
    assert segment_intersects_bbox([-1, 0], [2, 0], (0, -1, 1, 1))
    assert not segment_intersects_bbox([-1, 3], [2, 3], (0, -1, 1, 1))


def test_auto_view_spans_history_and_forecast(client, scope):
    scope.update(
        mode="auto",
        timeRange={
            "start": "2025-10-31T00:00:00+03:00",
            "end": "2025-11-02T00:00:00+03:00",
        },
    )
    response = client.post("/api/v1/map-snapshot", json=scope)
    assert response.status_code == 200
    result = response.json()
    assert result["meta"]["provenance"] == "mixed"
    assert len(result["frames"]) == 48
    assert {v["provenance"] for f in result["frames"][:24] for v in f["values"]} == {"observation"}
    assert {v["provenance"] for f in result["frames"][24:] for v in f["values"]} == {"forecast"}
    for mode, frames in (("history", result["frames"][:24]), ("forecast", result["frames"][24:])):
        expected = client.post(
            "/api/v1/map-snapshot",
            json={
                **scope,
                "mode": mode,
                "timeRange": {"start": frames[0]["start"], "end": frames[-1]["end"]},
            },
        ).json()
        assert frames == expected["frames"]
    for path in ("timeseries", "route-comparison", "heatmap"):
        part = client.post("/api/v1/" + path, json=scope)
        assert part.status_code == 200 and part.json()["meta"] == result["meta"]


def test_auto_january_uses_actual_data(client, scope):
    scope.update(
        mode="auto",
        timeRange={
            "start": "2025-01-01T00:00:00+03:00",
            "end": "2025-02-01T00:00:00+03:00",
        },
        grain="day",
    )
    response = client.post("/api/v1/map-snapshot", json=scope)
    assert response.status_code == 200
    result = response.json()
    assert len(result["frames"]) == 31
    assert result["meta"]["provenance"] == "observation"
    assert result["meta"]["forecastId"] is None
    assert all(f["aggregate"] is not None for f in result["frames"])
