"""Import dated OSM tram relations without inventing connections between stops."""

import json
from collections import defaultdict
from pathlib import Path

from .domain import ROUTES
from .storage import SnapshotStore, digest, file_hash

GEOGRAPHY_FILE = "moscow-trams-2025-01-01.json"


def ordered_path(ways, stop_ids):
    """Join only shared OSM nodes and verify the relation's stop order."""
    if not ways or len(stop_ids) < 2:
        raise ValueError("A route needs tram tracks and at least two stop positions")
    for first in (ways[0], list(reversed(ways[0]))):
        path = list(first)
        for nodes in ways[1:]:
            if path[-1] == nodes[0]:
                path.extend(nodes[1:])
            elif path[-1] == nodes[-1]:
                path.extend(reversed(nodes[:-1]))
            else:
                break
        else:
            positions = []
            try:
                for stop in stop_ids:
                    positions.append(path.index(stop, positions[-1] + 1 if positions else 0))
            except ValueError:
                continue
            return path, positions
    raise ValueError("Disconnected tram tracks or stop order inconsistent with the OSM route")


def prepare_osm_network(store: SnapshotStore, source: Path, *, publish=True, historical=False):
    sha = file_hash(source)
    ident = "network-" + digest({"sha256": sha, "geometrySchema": 2, "historical": historical})
    if store.path("networks", ident).exists():
        if publish:
            store.publish(networkId=ident)
        return store.read("networks", ident)
    document = json.loads(source.read_bytes())
    as_of = document["asOf"]
    elements = {(e["type"], e["id"]): e for e in document["data"]["elements"]}
    relations = sorted(
        (
            e
            for e in elements.values()
            if e["type"] == "relation"
            and e.get("tags", {}).get("route") == "tram"
            and e.get("tags", {}).get("ref", "").isdigit()
        ),
        key=lambda e: (int(e["tags"]["ref"]), e["id"]),
    )
    routes, patterns, stops, segments = {}, [], [], []
    directions = defaultdict(int)
    unavailable = []
    for relation in relations:
        tags = relation["tags"]
        route_id = tags["ref"]
        direction = directions[route_id]
        directions[route_id] += 1
        pid = f"pattern-osm-{relation['id']}"
        url = f"https://www.openstreetmap.org/relation/{relation['id']}"
        track_ways = [
            elements["way", member["ref"]]
            for member in relation["members"]
            if member["type"] == "way" and member["role"] in ("", "forward", "backward")
        ]
        if any(w.get("tags", {}).get("railway") != "tram" for w in track_ways):
            if not historical:
                raise ValueError(f"Non-tram way in {url}")
            unavailable.append(
                {
                    "routeId": route_id,
                    "sourceUrl": url,
                    "reason": "Non-tram or removed tracks in the archived relation",
                }
            )
            continue
        stop_ids = [
            m["ref"] for m in relation["members"] if m["type"] == "node" and m["role"].startswith("stop")
        ]
        try:
            path, positions = ordered_path([w["nodes"] for w in track_ways], stop_ids)
        except ValueError as error:
            if not historical:
                raise
            unavailable.append({"routeId": route_id, "sourceUrl": url, "reason": str(error)})
            continue

        def coordinate(node_id):
            node = elements["node", node_id]
            lon, lat = node["lon"], node["lat"]
            if not (37.25 <= lon <= 37.95 and 55.45 <= lat <= 55.95):
                raise ValueError(f"Route coordinate outside Moscow in {url}")
            return [lon, lat]

        visits = []
        for order, node_id in enumerate(stop_ids, 1):
            node = elements["node", node_id]
            visit = {
                "id": f"occ-osm-{relation['id']}-{order}",
                "stationId": f"osm-node-{node_id}",
                "name": node.get("tags", {}).get("name", f"Остановка OSM {node_id}"),
                "routeId": route_id,
                "patternId": pid,
                "direction": direction,
                "order": order,
                "coordinates": coordinate(node_id),
                "sourceUrl": f"https://www.openstreetmap.org/node/{node_id}",
            }
            visits.append(visit)
        stops.extend(visits)
        for i, (first, second) in enumerate(zip(visits, visits[1:])):
            segments.append(
                {
                    "id": f"segment-osm-{relation['id']}-{first['order']}",
                    "routeId": route_id,
                    "patternId": pid,
                    "fromId": first["id"],
                    "toId": second["id"],
                    "order": first["order"],
                    "coordinates": [coordinate(n) for n in path[positions[i] : positions[i + 1] + 1]],
                }
            )
        patterns.append(
            {
                "id": pid,
                "routeId": route_id,
                "direction": direction,
                "name": f"{visits[0]['name']} → {visits[-1]['name']}",
                "validFrom": as_of,
                "validTo": None,
                "observedAt": as_of,
                "sourceUrl": url,
            }
        )
        routes.setdefault(
            route_id,
            {
                "id": route_id,
                "number": route_id,
                "name": f"{tags.get('from', visits[0]['name'])} — {tags.get('to', visits[-1]['name'])}",
                "isTarget": int(route_id) in ROUTES,
                "hasData": int(route_id) in ROUTES,
                "hasGeometry": True,
                "historySupport": "no_positive_history"
                if route_id == "5"
                else "available"
                if int(route_id) in ROUTES
                else "unavailable",
                "sourceUrl": url,
                "officialUrl": tags.get("website"),
            },
        )
    missing = [str(r) for r in ROUTES if str(r) not in routes]
    if missing and not historical:
        raise ValueError(f"Missing target routes in geography extract: {missing}")
    for route_id in missing:
        routes[route_id] = {
            "id": route_id,
            "number": route_id,
            "name": f"Маршрут № {route_id}",
            "isTarget": True,
            "hasData": True,
            "hasGeometry": False,
            "historySupport": "no_positive_history" if route_id == "5" else "available",
        }
    network = {
        "networkSnapshotId": ident,
        "source": "OpenStreetMap tram route relations",
        "sourceHash": sha,
        "sourceUrl": "https://www.openstreetmap.org/copyright",
        "officialMapUrl": "https://transport.mos.ru/metro/trammap",
        "license": "ODbL-1.0",
        "geometryQuality": "mapped",
        "asOf": as_of,
        "routes": list(routes.values()),
        "patterns": patterns,
        "stops": stops,
        "segments": segments,
        "missingGeometryRouteIds": missing,
        "warning": f"Пути и остановки OpenStreetMap на {as_of}. Срез не подтверждает трассы на другие даты.",
        "checks": {
            "connectedPatterns": len(patterns),
            "straightLineFallbacks": 0,
            "stopOccurrences": len(stops),
            "trackVertices": sum(len(s["coordinates"]) for s in segments),
        },
        "unavailablePatterns": unavailable,
    }
    store.put("networks", ident, network)
    if publish:
        store.publish(networkId=ident)
    return network
