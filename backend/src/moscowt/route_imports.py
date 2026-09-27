"""CSV geometry import: validate first, publish immutable dated versions separately."""

import csv
import io
import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import date

from pydantic import Field

from .domain import ROUTES, DomainError, StrictModel
from .storage import SnapshotStore, digest

COLUMNS = [
    "route_id",
    "route_name",
    "direction",
    "valid_from",
    "valid_to",
    "point_order",
    "longitude",
    "latitude",
    "stop_id",
    "stop_name",
]


class RouteImportRequest(StrictModel):
    snapshotId: str
    filename: str = Field(min_length=1, max_length=200)
    csv: str = Field(min_length=1, max_length=5_000_000)


class ApplyImportRequest(StrictModel):
    snapshotId: str


def invalid(message):
    raise DomainError("INVALID_ROUTE_CSV", message)


def parse_route_csv(text: str, filename: str):
    if len(text.encode("utf-8")) > 5_000_000:
        invalid("Файл должен быть не больше 5 МБ")
    text = text.removeprefix("\ufeff")
    if not text.strip():
        invalid("CSV пуст")
    delimiter = ";" if ";" in text.splitlines()[0] else ","
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
        if reader.fieldnames != COLUMNS:
            invalid("Столбцы должны совпадать с шаблоном: " + ";".join(COLUMNS))
        rows = list(reader)
    except csv.Error:
        invalid("Повреждён CSV: проверьте кавычки и разделители")
    if not 2 <= len(rows) <= 20000:
        invalid("Допустимо от 2 до 20 000 точек трассы")
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        invalid("Количество значений в строке не совпадает с заголовком")
    rows = [{k: v.strip() for k, v in row.items()} for row in rows]
    common = ["route_id", "route_name", "valid_from", "valid_to"]
    if any(any(row[k] != rows[0][k] for k in common) for row in rows):
        invalid("Один файл должен содержать один маршрут и один период действия")
    head = rows[0]
    rid = head["route_id"]
    if not re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё_-]{1,16}", rid):
        invalid("Номер маршрута: до 16 букв, цифр, дефисов или подчёркиваний")
    if not 1 <= len(head["route_name"]) <= 160:
        invalid("Название маршрута должно содержать от 1 до 160 символов")
    for field in ("valid_from", "valid_to"):
        try:
            if date.fromisoformat(head[field]).isoformat() != head[field]:
                raise ValueError
        except ValueError:
            invalid("Даты должны иметь формат YYYY-MM-DD")
    if not head["valid_from"] < head["valid_to"]:
        invalid("valid_to должен быть позже valid_from; это первый день без этой версии")
    groups = defaultdict(list)
    station_positions = {}
    for line, row in enumerate(rows, 2):
        try:
            if row["direction"] not in ("0", "1") or not re.fullmatch(r"[1-9]\d{0,5}", row["point_order"]):
                raise ValueError
            point = [float(row["longitude"]), float(row["latitude"])]
            if not (37.25 <= point[0] <= 37.95 and 55.45 <= point[1] <= 55.95):
                raise ValueError
        except ValueError:
            invalid(
                f"Строка {line}: direction — 0/1, point_order — целое от 1, координаты — долгота/широта Москвы"
            )
        if (
            bool(row["stop_id"]) != bool(row["stop_name"])
            or len(row["stop_name"]) > 160
            or len(row["stop_id"]) > 80
        ):
            invalid(f"Строка {line}: для остановки нужны stop_id и stop_name (до 80 и 160 символов)")
        if row["stop_id"]:
            station = (row["stop_name"], point)
            if row["stop_id"] in station_positions and station_positions[row["stop_id"]] != station:
                invalid(
                    f"Строка {line}: один stop_id должен обозначать одну остановку с одинаковыми координатами"
                )
            station_positions[row["stop_id"]] = station
        groups[int(row["direction"])].append((int(row["point_order"]), point, row))
    ident = "import-" + digest({"rows": rows, "filename": filename})
    patterns, stops, segments = [], [], []
    for direction, points in sorted(groups.items()):
        points.sort(key=lambda x: x[0])
        if [p[0] for p in points] != list(range(1, len(points) + 1)):
            invalid(f"Направление {direction}: point_order должен идти от 1 без пропусков и повторов")
        named = [i for i, point in enumerate(points) if point[2]["stop_id"]]
        if len(named) < 2 or named[0] != 0 or named[-1] != len(points) - 1:
            invalid(f"Направление {direction}: нужны минимум две остановки, включая первую и последнюю точки")
        for a, b in zip(points, points[1:]):
            lon, lat = a[1]
            dx = (b[1][0] - lon) * 111_320 * math.cos(math.radians(lat))
            dy = (b[1][1] - lat) * 111_320
            if math.hypot(dx, dy) > 1000:
                invalid(
                    f"Направление {direction}, точка {b[0]}: разрыв более 1 км; добавьте промежуточные точки трассы"
                )
        pid = f"pattern-{ident}-{direction}"
        visits = []
        for order, i in enumerate(named, 1):
            _, coordinate, row = points[i]
            visits.append(
                {
                    "id": f"occ-{ident}-{direction}-{order}",
                    "stationId": f"csv-{row['stop_id']}",
                    "name": row["stop_name"],
                    "routeId": rid,
                    "patternId": pid,
                    "direction": direction,
                    "order": order,
                    "coordinates": coordinate,
                }
            )
        for i, (a, b) in enumerate(zip(visits, visits[1:])):
            segments.append(
                {
                    "id": f"segment-{ident}-{direction}-{i + 1}",
                    "routeId": rid,
                    "patternId": pid,
                    "fromId": a["id"],
                    "toId": b["id"],
                    "order": i + 1,
                    "coordinates": [p[1] for p in points[named[i] : named[i + 1] + 1]],
                }
            )
        stops.extend(visits)
        patterns.append(
            {
                "id": pid,
                "routeId": rid,
                "direction": direction,
                "name": f"{visits[0]['name']} → {visits[-1]['name']}",
                "validFrom": head["valid_from"],
                "validTo": head["valid_to"],
                "observedAt": head["valid_from"],
                "sourceKind": "user_csv",
                "importId": ident,
            }
        )
    target = rid in {str(r) for r in ROUTES}
    return {
        "id": ident,
        "filename": filename,
        "validFrom": head["valid_from"],
        "validTo": head["valid_to"],
        "route": {
            "id": rid,
            "number": rid,
            "name": head["route_name"],
            "isTarget": target,
            "hasData": target,
            "hasGeometry": True,
            "sourceKind": "user_csv",
            "historySupport": "no_positive_history"
            if rid == "5"
            else "available"
            if target
            else "unavailable",
        },
        "patterns": patterns,
        "stops": stops,
        "segments": segments,
        "pointCount": len(rows),
    }


def preview_import(store: SnapshotStore, body: RouteImportRequest):
    snapshot = store.read("snapshots", body.snapshotId)
    archive = store.read("networks", snapshot["networkId"])
    if not archive.get("versions"):
        raise DomainError("HISTORY_NOT_READY", "Сначала подготовьте архив геометрии 2025 года", 409)
    item = parse_route_csv(body.csv, body.filename)
    store.put("route_imports", item["id"], item)
    directions = {p["direction"] for p in item["patterns"]}
    overlaps = []
    for entry in archive.get("imports", []):
        other = store.read("route_imports", entry["id"])
        if (
            other["route"]["id"] == item["route"]["id"]
            and directions.intersection(p["direction"] for p in other["patterns"])
            and item["validFrom"] < other["validTo"]
            and other["validFrom"] < item["validTo"]
        ):
            overlaps.append(other["filename"])
    return {
        "id": item["id"],
        "route": item["route"],
        "validFrom": item["validFrom"],
        "validTo": item["validTo"],
        "directions": sorted(directions),
        "stopCount": len(item["stops"]),
        "pointCount": item["pointCount"],
        "replacesExisting": any(r["id"] == item["route"]["id"] for r in archive["routes"])
        or any(
            store.read("route_imports", e["id"])["route"]["id"] == item["route"]["id"]
            for e in archive.get("imports", [])
        ),
        "overlappingImports": overlaps,
        "warning": "Пользовательская трасса из CSV. Геометрия не проверена перевозчиком; значения пассажиропотока не меняются.",
    }


def apply_import(store: SnapshotStore, ident: str, expected_snapshot: str):
    item = store.read("route_imports", ident)
    # Serialize whole read-modify-publish operation so simultaneous uploads cannot be lost.
    with store.lock("route-import"):
        current = store.current()
        archive = store.read("networks", current["networkId"])
        if any(e["id"] == ident for e in archive.get("imports", [])):
            return {
                "snapshotId": current["snapshotId"],
                "networkSnapshotId": current["networkId"],
                "routeId": item["route"]["id"],
                "validFrom": item["validFrom"],
            }
        expected = store.read("snapshots", expected_snapshot)
        if expected.get("networkId") != current.get("networkId"):
            raise DomainError(
                "NETWORK_CHANGED", "Сеть изменилась. Обновите страницу и проверьте CSV повторно", 409
            )
        if not archive.get("versions"):
            raise DomainError("HISTORY_NOT_READY", "Архив 2025 года не подготовлен", 409)
        value = deepcopy(archive)
        value["baseArchiveId"] = archive.get("baseArchiveId", archive["networkSnapshotId"])
        value["imports"].append({"id": ident})
        value["networkSnapshotId"] = "network-" + digest(
            {"base": value["baseArchiveId"], "imports": value["imports"]}
        )
        store.put("networks", value["networkSnapshotId"], value)
        published = store.publish(networkId=value["networkSnapshotId"])
        return {
            "snapshotId": published["snapshotId"],
            "networkSnapshotId": value["networkSnapshotId"],
            "routeId": item["route"]["id"],
            "validFrom": item["validFrom"],
        }
