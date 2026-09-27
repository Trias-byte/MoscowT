import json

import pytest

from moscowt.analytics import NetworkCatalogService
from moscowt.domain import ROUTES, Scope
from moscowt.geography import ordered_path
from moscowt.network_history import resolve_network


def test_all_archive_versions_follow_only_source_tram_tracks(store, dataset):
    archive = store.read("networks", store.current()["networkId"])
    assert len(archive["versions"]) >= 20
    for version in archive["versions"]:
        network = store.read("networks", version["networkId"])
        raw = json.loads(
            (dataset / "geography/history-2025" / f"moscow-trams-{version['validFrom']}.json").read_bytes()
        )
        check_source_edges(network, raw)


def check_source_edges(network, raw):
    elements = {(e["type"], e["id"]): e for e in raw["data"]["elements"]}
    assert network["geometryQuality"] == "mapped"
    assert network["asOf"] == raw["asOf"]
    assert {r["id"] for r in network["routes"] if r["isTarget"]} == {str(r) for r in ROUTES}
    stops = {s["id"]: s for s in network["stops"]}

    def coordinate(node):
        e = elements["node", node]
        return (e["lon"], e["lat"])

    for pattern in network["patterns"]:
        relation_id = int(pattern["id"].removeprefix("pattern-osm-"))
        relation = elements["relation", relation_id]
        edges = set()
        for member in relation["members"]:
            if member["type"] != "way" or member["role"] not in ("", "forward", "backward"):
                continue
            way = elements["way", member["ref"]]
            assert way["tags"]["railway"] == "tram"
            for a, b in zip(way["nodes"], way["nodes"][1:]):
                edges.add((coordinate(a), coordinate(b)))
                edges.add((coordinate(b), coordinate(a)))
        segments = [s for s in network["segments"] if s["patternId"] == pattern["id"]]
        assert len(segments) >= 1
        assert any(len(s["coordinates"]) > 10 for s in segments)
        for segment in segments:
            points = [tuple(p) for p in segment["coordinates"]]
            assert all((a, b) in edges for a, b in zip(points, points[1:]))
            assert segment["coordinates"][0] == stops[segment["fromId"]]["coordinates"]
            assert segment["coordinates"][-1] == stops[segment["toId"]]["coordinates"]
    for stop in stops.values():
        node = elements["node", int(stop["stationId"].removeprefix("osm-node-"))]
        assert stop["name"] == node["tags"]["name"]
        assert stop["coordinates"] == [node["lon"], node["lat"]]


def test_disconnected_tracks_and_invalid_stop_order_are_rejected():
    with pytest.raises(ValueError, match="Disconnected"):
        ordered_path([[1, 2], [3, 4]], [1, 4])
    with pytest.raises(ValueError, match="stop order"):
        ordered_path([[1, 2, 3, 4]], [1, 3, 2, 4])
    assert ordered_path([[3, 2, 1], [3, 4, 5]], [1, 4, 5]) == ([1, 2, 3, 4, 5], [0, 3, 4])


def test_date_selection_preserves_source_metadata_and_geometry(store):
    from moscowt.storage import canonical

    archive = store.read("networks", store.current()["networkId"])
    before = {v["networkId"]: canonical(store.read("networks", v["networkId"])) for v in archive["versions"]}
    first = resolve_network(store, archive, "2025-11-01")
    expected = canonical(first)
    resolve_network(store, archive, "2026-01-01")
    resolve_network(store, archive, "2025-12-31")
    assert canonical(first) == expected
    assert canonical(resolve_network(store, archive, "2025-11-01")) == expected
    assert all(canonical(store.read("networks", ident)) == content for ident, content in before.items())


def test_bbox_checks_all_vertices_of_curved_segments(store, scope):
    network = resolve_network(store, store.read("networks", store.current()["networkId"]), "2025-11-01")
    segment = next(s for s in network["segments"] if len(s["coordinates"]) > 10)
    lon, lat = segment["coordinates"][len(segment["coordinates"]) // 2]
    scope.update(
        routeIds=[segment["routeId"]],
        geometry={"bbox": [lon - 0.00001, lat - 0.00001, lon + 0.00001, lat + 0.00001]},
    )
    geometry = NetworkCatalogService(store).geometry(Scope.model_validate(scope))
    assert segment["id"] in geometry["segmentIds"]
    assert geometry["geometryQuality"] == "mapped"


def test_geometry_date_can_differ_from_numeric_period_and_never_uses_2026(store, scope):
    service = NetworkCatalogService(store)
    archive = store.read("networks", store.current()["networkId"])
    scope["geometry"] = {"date": "2025-12-31"}
    selected = service.geometry(Scope.model_validate(scope))
    assert selected["asOf"] == "2025-12-31"
    assert "5" not in selected["missingGeometryRouteIds"]
    scope["geometry"] = {"date": "2025-12-15"}
    assert "5" in service.geometry(Scope.model_validate(scope))["missingGeometryRouteIds"]
    scope["geometry"] = {"date": "2025-12-16"}
    assert "5" not in service.geometry(Scope.model_validate(scope))["missingGeometryRouteIds"]
    opening = resolve_network(store, archive, "2025-12-16")
    assert next(r for r in opening["routes"] if r["id"] == "5")["sourceKind"] == "reconstruction"
    assert all(p["observedAt"] == "2025-12-19" for p in opening["patterns"] if p["routeId"] == "5")
    assert len({s["id"] for s in opening["stops"]}) == len(opening["stops"])
    assert any(p["routeId"] == "9" for p in opening["patterns"])
    assert all(p["routeId"] != "9" for p in resolve_network(store, archive, "2025-12-17")["patterns"])
    for day in ("2025-01-01", "2025-07-01", "2025-12-31"):
        network = resolve_network(store, archive, day)
        assert network["asOf"] == day
        assert network["sourceAsOf"].startswith("2025") and network["sourceAsOf"] <= day
        assert all(p["observedAt"].startswith("2025") for p in network["patterns"])


def test_archive_changes_geometry_during_year(store):
    archive = store.read("networks", store.current()["networkId"])
    first = resolve_network(store, archive, "2025-01-01")
    last = resolve_network(store, archive, "2025-12-31")
    assert first["segments"] != last["segments"]
    assert any(
        [s["name"] for s in first["stops"] if s["routeId"] == rid]
        != [s["name"] for s in last["stops"] if s["routeId"] == rid]
        for rid in ("7", "12", "50")
    )
