"""Derive directional route connections from the dated OSM source, without traffic labels.

The unit is a pair of route patterns and a common stop from which both may board
and follow the identical directed track to another common alighting stop.
Only the ten target routes are included. Run from any directory with Python 3.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET_ROUTES = (1, 5, 7, 11, 12, 17, 25, 26, 28, 50)


@dataclass
class Pattern:
    route: int
    relation: int
    direction: int
    path: list[int]
    stops: list[dict]

    @property
    def destination(self):
        return self.stops[-1]["name"]


def ordered_path(ways, stop_ids):
    for first in (ways[0], list(reversed(ways[0]))):
        path = list(first)
        for way in ways[1:]:
            if path[-1] == way[0]:
                path.extend(way[1:])
            elif path[-1] == way[-1]:
                path.extend(reversed(way[:-1]))
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
    raise ValueError("Disconnected OSM ways or inconsistent stop order")


def load_patterns(document):
    elements = {(e["type"], e["id"]): e for e in document["data"]["elements"]}
    nodes = {ident: e for (kind, ident), e in elements.items() if kind == "node"}
    relations = sorted(
        [e for (kind, _), e in elements.items() if kind == "relation"
         and e.get("tags", {}).get("route") == "tram"
         and int(e["tags"]["ref"]) in TARGET_ROUTES],
        key=lambda e: (int(e["tags"]["ref"]), e["id"]),
    )
    directions = Counter()
    patterns = []
    for rel in relations:
        route = int(rel["tags"]["ref"])
        ways = [elements["way", m["ref"]] for m in rel["members"]
                if m["type"] == "way" and m["role"] in ("", "forward", "backward")]
        assert ways and all(w.get("tags", {}).get("railway") == "tram" for w in ways)
        members = [m for m in rel["members"] if m["type"] == "node" and m["role"].startswith("stop")]
        path, positions = ordered_path([w["nodes"] for w in ways], [m["ref"] for m in members])
        stops = [dict(node_id=m["ref"], name=nodes[m["ref"]]["tags"]["name"],
                      order=i + 1, position=pos, role=m["role"])
                 for i, (m, pos) in enumerate(zip(members, positions))]
        patterns.append(Pattern(route, rel["id"], directions[route], path, stops))
        directions[route] += 1
    assert sorted(directions) == list(TARGET_ROUTES)
    assert all(count == 2 for count in directions.values())
    return patterns, nodes


def meters(first, second):
    lat1, lat2 = math.radians(first["lat"]), math.radians(second["lat"])
    dlat, dlon = lat2 - lat1, math.radians(second["lon"] - first["lon"])
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(a)))


def common_forward_path(a, sa, b, sb):
    pa, pb = sa["position"], sb["position"]
    end_a, end_b = a.stops[-1]["position"], b.stops[-1]["position"]
    count = 0
    while pa + count <= end_a and pb + count <= end_b and a.path[pa + count] == b.path[pb + count]:
        count += 1
    return a.path[pa:pa + count]


def analyze(patterns, nodes, as_of):
    rows, exclusions = [], []
    shared_edges = defaultdict(set)
    for a, b in itertools.combinations(patterns, 2):
        if a.route == b.route:
            continue
        # Pattern order from load_patterns makes route_a < route_b reproducible.
        for sa, sb in itertools.product(a.stops, b.stops):
            if sa["node_id"] != sb["node_id"]:
                continue
            evidence = dict(route_a=a.route, route_b=b.route,
                            relation_a=a.relation, relation_b=b.relation,
                            direction_a=a.direction, direction_b=b.direction,
                            stop_id=f'osm-node-{sa["node_id"]}', stop_name=sa["name"])
            if sa["role"] == "stop_exit_only" or sb["role"] == "stop_exit_only":
                exclusions.append({**evidence, "reason": "alighting_only_at_origin"})
                continue
            path = common_forward_path(a, sa, b, sb)
            if len(path) < 2:
                exclusions.append({**evidence, "reason": "different_outgoing_track_or_terminal"})
                continue
            # Stops must lie at the same offset along the EXACT shared node sequence.
            # This allows one route to skip an intermediate stop while retaining proof.
            candidates = []
            for ta in a.stops:
                offset = ta["position"] - sa["position"]
                if not 0 < offset < len(path) or ta["role"] == "stop_entry_only":
                    continue
                for tb in b.stops:
                    if (tb["node_id"] == ta["node_id"]
                        and tb["position"] - sb["position"] == offset
                        and tb["role"] != "stop_entry_only"):
                        candidates.append((offset, ta, tb))
            if not candidates:
                exclusions.append({**evidence, "reason": "no_common_alighting_stop_before_divergence"})
                continue
            candidates.sort(key=lambda item: item[0])
            offset, ta, tb = candidates[0]
            end_offset, end_a, _ = candidates[-1]
            distances = [meters(nodes[x], nodes[y]) for x, y in zip(path, path[1:])]
            edges = list(zip(path[:end_offset], path[1:end_offset + 1]))
            shared_edges[a.route].update(edges)
            shared_edges[b.route].update(edges)
            rows.append({
                **evidence,
                "towards_a": a.destination, "towards_b": b.destination,
                "stop_order_a": sa["order"], "stop_order_b": sb["order"],
                "stop_lat": nodes[sa["node_id"]]["lat"], "stop_lon": nodes[sa["node_id"]]["lon"],
                "next_common_stop_id": f'osm-node-{ta["node_id"]}',
                "next_common_stop_name": ta["name"],
                "next_stop_order_a": ta["order"], "next_stop_order_b": tb["order"],
                "same_immediate_next_stop": int(ta["order"] == sa["order"] + 1 and tb["order"] == sb["order"] + 1),
                "shared_to_next_stop_m": round(sum(distances[:offset]), 1),
                "corridor_end_stop_id": f'osm-node-{end_a["node_id"]}',
                "corridor_end_stop_name": end_a["name"],
                "remaining_common_corridor_m": round(sum(distances[:end_offset]), 1),
                "downstream_common_stop_count": len(candidates),
                "same_direction": 1,
                "source": "OpenStreetMap", "geometry_as_of": as_of,
                "historical_2025_verified": 0,
            })
    rows.sort(key=lambda r: (r["route_a"], r["route_b"], r["relation_a"], r["relation_b"], r["stop_order_a"]))
    return rows, exclusions, shared_edges


def csv_file(path, rows, fields=None):
    assert rows or fields
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "dataset/geography/moscow-trams-2026-02-09.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dataset/derived/route_connections")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    document = json.loads(raw)
    patterns, nodes = load_patterns(document)
    rows, exclusions, edges = analyze(patterns, nodes, document["asOf"])
    keys = [(r["relation_a"], r["relation_b"], r["stop_order_a"], r["stop_order_b"]) for r in rows]
    assert len(keys) == len(set(keys))
    assert all(r["shared_to_next_stop_m"] > 0 and r["same_direction"] == 1 for r in rows)
    assert all(r["route_a"] < r["route_b"] for r in rows)

    features = []
    for route in TARGET_ROUTES:
        related = [r for r in rows if route in (r["route_a"], r["route_b"])]
        peers = sorted({r["route_b"] if r["route_a"] == route else r["route_a"] for r in related})
        features.append(dict(
            route=route, same_direction_peer_count=len(peers),
            same_direction_peer_routes="|".join(map(str, peers)),
            shared_departure_stop_positions=len({r["stop_id"] for r in related}),
            shared_stop_pair_direction_rows=len(related),
            shared_directed_track_m=round(sum(meters(nodes[x], nodes[y]) for x, y in sorted(edges[route])), 1),
            geometry_as_of=document["asOf"], historical_2025_verified=0,
        ))

    groups = defaultdict(list)
    for row in rows:
        groups[row["route_a"], row["route_b"], row["relation_a"], row["relation_b"]].append(row)
    corridors = []
    for key, items in groups.items():
        # A corridor terminates where its last usable common alighting stop does.
        runs = defaultdict(list)
        for item in items:
            runs[item["corridor_end_stop_id"]].append(item)
        for run in runs.values():
            first = run[0]
            corridors.append(dict(route_a=first["route_a"], route_b=first["route_b"],
                relation_a=first["relation_a"], relation_b=first["relation_b"],
                direction_a=first["direction_a"], direction_b=first["direction_b"],
                towards_a=first["towards_a"], towards_b=first["towards_b"],
                shared_departure_stops=len(run),
                departure_stop_ids="|".join(r["stop_id"] for r in run),
                departure_stop_names=" | ".join(r["stop_name"] for r in run),
                end_stop_id=first["corridor_end_stop_id"],
                end_stop_name=first["corridor_end_stop_name"],
                corridor_m=first["remaining_common_corridor_m"],
                geometry_as_of=document["asOf"], historical_2025_verified=0))

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    csv_file(out / "same_direction_stops.csv", rows)
    csv_file(out / "route_connection_features.csv", features)
    csv_file(out / "same_direction_corridors.csv", corridors)
    csv_file(out / "excluded_common_stops.csv", exclusions)
    metadata = dict(source=str(args.source.relative_to(ROOT)),
        source_sha256=hashlib.sha256(raw).hexdigest(), geometry_as_of=document["asOf"],
        target_routes=list(TARGET_ROUTES), route_pairs=len({(r["route_a"], r["route_b"]) for r in rows}),
        pattern_pairs=len(groups), stop_pair_direction_rows=len(rows),
        unique_departure_stop_positions=len({r["stop_id"] for r in rows}),
        corridors=len(corridors), excluded_stop_pair_direction_rows=len(exclusions),
        exclusion_reasons=dict(Counter(r["reason"] for r in exclusions)),
        source_license="ODbL-1.0", historical_2025_verified=False,
        rule="same OSM stop node, boarding permitted by member roles, identical forward track node sequence to a common alighting stop; direction numbers are not compared",
        note="Static dated route topology; no event-to-stop matching or historical 2025 validity is asserted.")
    (out / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    (out / "workbook-input.json").write_text(json.dumps(dict(metadata=metadata, stops=rows, features=features, corridors=corridors), ensure_ascii=False))

    duplicate_names = {name for name in {nodes[int(r['stop_id'].removeprefix('osm-node-'))]['tags']['name'] for r in rows}
                       if len({r['stop_id'] for r in rows if r['stop_name'] == name}) > 2}
    def stop_label(name, stop_id):
        return f"{name} [{stop_id}]" if name in duplicate_names else name

    lines = ["# Общие остановки и движение в одном направлении", "",
        f"Срез OpenStreetMap: **{document['asOf']}**. Только десять целевых маршрутов.", "",
        f"Таблица содержит **{len(rows)} строки: остановка × пара направлений** для **{metadata['route_pairs']} пар маршрутов**.", "",
        "Ниже перечислены точки, откуда оба маршрута могут отправиться по одному пути до следующей общей остановки. "
        "Последняя общая остановка указана отдельно: после неё маршруты могут разойтись или завершить движение.", "",
        "Движение совпадает по последовательности узлов рельсов. Числа direction_a и direction_b — локальные обозначения; они могут различаться.", ""]
    last_pair = None
    for c in corridors:
        pair = c["route_a"], c["route_b"]
        if pair != last_pair:
            lines += [f"## Маршруты {pair[0]} и {pair[1]}", ""]
            last_pair = pair
        run = [r for r in groups[c['route_a'], c['route_b'], c['relation_a'], c['relation_b']]
               if r['corridor_end_stop_id'] == c['end_stop_id']]
        distance = f"{c['corridor_m'] / 1000:.2f}".replace('.', ',')
        end_label = stop_label(c['end_stop_name'], run[0]['corridor_end_stop_id'])
        lines += [f"**№ {pair[0]}: в сторону {c['towards_a']}; № {pair[1]}: в сторону {c['towards_b']}.**", "",
            "Общие точки отправления по порядку: " + "; ".join(stop_label(r['stop_name'], r['stop_id']) for r in run) + ".", "",
            f"Последняя общая остановка: **{end_label}**. Общий участок около {distance} км.", ""]
    isolated = [str(f["route"]) for f in features if not f["same_direction_peer_count"]]
    lines += ["## Применение к датасету", "",
        "У маршрутов " + ", ".join(isolated) + " нет таких связей с другими целевыми маршрутами в этом срезе.", "",
        "№ 7 и 25 у метро «Сокольники» имеют разные остановочные точки. Близость на карте не включается в список совпадающих остановок.", "",
        "В train.csv/test.csv нет stop_id, координат события и направления. Эта таблица описывает сеть, а не место конкретной валидации.", "",
        "Для маршрутных признаков присоединять route_connection_features.csv по route с проверкой many_to_one. "
        "Подробную таблицу напрямую к labels не присоединять: это размножит маршрутные строки и пассажиропоток.", "",
        "Все строки имеют historical_2025_verified = 0. География на февраль 2026 не подтверждает сеть 2025 года. "
        "До проверки исторической географии использовать эти признаки только в явно отмеченном эксперименте; "
        "не представлять его как проверку без использования более поздней информации.", "",
        "Источник: © OpenStreetMap contributors, ODbL 1.0. Идентификаторы osm-node-* не являются stop_id из исходного Excel.", ""]
    (out / "STATIONS.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
