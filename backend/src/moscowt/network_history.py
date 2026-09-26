"""Immutable geography archives and effective-date selection (Moscow calendar days)."""

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

from .domain import ROUTES, DomainError
from .geography import prepare_osm_network
from .storage import SnapshotStore, digest

HISTORY_INDEX = "history-2025/index.json"
HISTORY_WARNING = (
    "Архив OSM за 2025 год: версия выбирается по дате. Даты правок карты могут отличаться "
    "от дат изменения движения; временные изменения могут отсутствовать. "
    "Подложка OpenStreetMap современная, архивными являются трассы и остановки."
)


def prepare_history(store: SnapshotStore, directory: Path):
    index = json.loads((directory / HISTORY_INDEX).read_bytes())
    versions = []
    for day in index["dates"]:
        source = directory / "history-2025" / f"moscow-trams-{day}.json"
        value = prepare_osm_network(store, source, publish=False, historical=True)
        versions.append({"validFrom": day, "networkId": value["networkSnapshotId"]})
    for i, version in enumerate(versions):
        version["validTo"] = versions[i + 1]["validFrom"] if i + 1 < len(versions) else index["end"]
    archive = deepcopy(store.read("networks", versions[0]["networkId"]))
    dates_path = directory / "history-2025/service-dates.json"
    service_dates = json.loads(dates_path.read_bytes()) if dates_path.exists() else {}
    archive.update(
        versions=versions,
        historyRange={"start": index["start"], "end": index["end"]},
        historyBasis=index["basis"],
        warning=HISTORY_WARNING,
        imports=[],
        serviceDates=service_dates,
    )
    archive["networkSnapshotId"] = "network-" + digest(
        {"versions": versions, "serviceDates": service_dates, "archiveSchema": 2}
    )
    store.put("networks", archive["networkSnapshotId"], archive)
    # Keep CSV additions when rebuilding the same archive.
    current = store.current().get("networkId")
    if current:
        previous = store.read("networks", current)
        if previous.get("baseArchiveId") == archive["networkSnapshotId"]:
            return previous
    store.publish(networkId=archive["networkSnapshotId"])
    return archive


def resolve_network(store: SnapshotStore, archive: dict, day: str | None = None):
    if not archive.get("versions"):
        return archive
    day = day or archive["historyRange"]["start"]
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError:
        raise DomainError("INVALID_GEOMETRY_DATE", "Ожидается дата YYYY-MM-DD") from None
    version = next((v for v in archive["versions"] if v["validFrom"] <= day < v["validTo"]), None)
    if not version:
        raise DomainError("GEOMETRY_DATE_UNAVAILABLE", "Архив маршрутов доступен с 01.01 по 31.12.2025")
    value = deepcopy(store.read("networks", version["networkId"]))
    value.update(
        networkSnapshotId=archive["networkSnapshotId"],
        asOf=day,
        sourceAsOf=version["validFrom"],
        historyRange=archive["historyRange"],
        historyBasis=archive["historyBasis"],
        warning=HISTORY_WARNING,
        imports=[],
    )
    for pattern in value["patterns"]:
        pattern.update(validFrom=version["validFrom"], validTo=version["validTo"], sourceKind="osm_archive")
    # Official opening takes precedence over an earlier planned OSM relation.
    if day < "2025-12-16":
        remove_patterns(value, {p["id"] for p in value["patterns"] if p["routeId"] == "5"})
    for rule in archive.get("serviceDates", {}).get("reconstructions", []):
        if rule["validFrom"] <= day < rule["validTo"]:
            source_version = next(v for v in archive["versions"] if v["validFrom"] == rule["geometryDate"])
            source = store.read("networks", source_version["networkId"])
            rid = rule["routeId"]
            remove_patterns(value, {p["id"] for p in value["patterns"] if p["routeId"] == rid})
            route = deepcopy(next(r for r in source["routes"] if r["id"] == rid))
            route.update(
                geometryNote=rule["note"], officialUrl=rule["officialUrl"], sourceKind="reconstruction"
            )
            value["routes"] = [r for r in value["routes"] if r["id"] != rid] + [route]
            for kind in ("patterns", "stops", "segments"):
                for element in source[kind]:
                    if element["routeId"] != rid:
                        continue
                    element = deepcopy(element)
                    for key in ("id", "patternId", "fromId", "toId"):
                        if key in element:
                            element[key] += "-reconstructed"
                    if kind == "patterns":
                        element.update(
                            validFrom=rule["validFrom"],
                            validTo=rule["validTo"],
                            sourceKind="reconstruction",
                            sourceNote=rule["note"],
                        )
                    value[kind].append(element)
    for rule in archive.get("serviceDates", {}).get("retirements", []):
        if day >= rule["validFrom"]:
            remove_patterns(value, {p["id"] for p in value["patterns"] if p["routeId"] == rule["routeId"]})
    for entry in archive.get("imports", []):
        item = store.read("route_imports", entry["id"])
        if not item["validFrom"] <= day < item["validTo"]:
            continue
        directions = {p["direction"] for p in item["patterns"]}
        remove_patterns(
            value,
            {
                p["id"]
                for p in value["patterns"]
                if p["routeId"] == item["route"]["id"] and p["direction"] in directions
            },
        )
        value["routes"] = [r for r in value["routes"] if r["id"] != item["route"]["id"]] + [item["route"]]
        for kind in ("patterns", "stops", "segments"):
            value[kind].extend(deepcopy(item[kind]))
        value["imports"].append(
            {"id": item["id"], "filename": item["filename"], "routeId": item["route"]["id"]}
        )
    available = {p["routeId"] for p in value["patterns"]}
    value["missingGeometryRouteIds"] = [str(r) for r in ROUTES if str(r) not in available]
    for route in value["routes"]:
        route["hasGeometry"] = route["id"] in available
        if route["id"] == "5" and day < "2025-12-16" and route.get("sourceKind") != "user_csv":
            route["geometryNote"] = "Открыт 16 декабря 2025 года"
            route["officialUrl"] = "https://mosmetro.ru/news/details/8515"
    value["routes"].sort(
        key=lambda r: (not r["id"].isdigit(), int(r["id"]) if r["id"].isdigit() else r["id"])
    )
    return value


def remove_patterns(network, ids):
    network["patterns"] = [p for p in network["patterns"] if p["id"] not in ids]
    for kind in ("stops", "segments"):
        network[kind] = [e for e in network[kind] if e["patternId"] not in ids]
