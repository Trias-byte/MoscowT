"""Cache the 2025 OSM relation history and dated extracts; no requests at app runtime.

OSM edit dates are evidence of mapping changes, not official service change dates.
"""

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2] / "dataset/geography"
OUT = ROOT / "history-2025"
ENDPOINT = os.environ.get("OSM_OVERPASS_ENDPOINT", "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
BATCH_SIZE = int(os.environ.get("OSM_BATCH_SIZE", "30"))
IDS = [
    448785,
    540033,
    540139,
    543080,
    556900,
    918052,
    920053,
    1082824,
    1224026,
    1224136,
    1225388,
    1244690,
    1283310,
    1284062,
    1343391,
    1538169,
    1538170,
    1689026,
    1689064,
    3184022,
    3184023,
    3186264,
    3186265,
    3299879,
    7556406,
    14258874,
    14258875,
    15840810,
    18088223,
    18088224,
]


def fetch(url):
    for attempt in range(3):
        try:
            with urlopen(
                Request(url, headers={"User-Agent": "MoscowT-local-map/1.0", "Accept-Encoding": "gzip"}),
                timeout=70,
            ) as r:
                raw = r.read()
                value = json.loads(
                    gzip.decompress(raw) if r.headers.get("Content-Encoding") == "gzip" else raw
                )
            if value.get("remark"):
                raise ValueError(value["remark"])
            return value
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def main():
    OUT.mkdir(exist_ok=True)
    days = {"2025-01-01", "2025-12-16", "2025-12-31"}
    for ident in IDS:
        path = OUT / f"relation-{ident}.json"
        url = f"https://api.openstreetmap.org/api/0.6/relation/{ident}/history.json"
        if not path.exists():
            elements = fetch(url)["elements"]
            before = [e for e in elements if e["timestamp"] < "2025"]
            during = [e for e in elements if "2025" <= e["timestamp"] < "2026"]
            kept = before[-1:] + during
            for element in kept:
                for field in ("uid", "user", "changeset"):
                    element.pop(field, None)
            path.write_text(json.dumps({"sourceUrl": url, "elements": kept}, ensure_ascii=False) + "\n")
        elements = json.loads(path.read_text())["elements"]
        for element in elements:
            # Daily archive uses the OSM database at the end of the Moscow day.
            from zoneinfo import ZoneInfo

            day = datetime.fromisoformat(element["timestamp"]).astimezone(ZoneInfo("Europe/Moscow"))
            if day.year == 2025:
                days.add(day.date().isoformat())
        print(f"History {ident}: {len(elements)} versions", flush=True)
    for day in sorted(days):
        path = OUT / f"moscow-trams-{day}.json"
        if path.exists():
            continue
        queries, elements = [], {}
        # Small cached batches also tolerate gateways that truncate large JSON replies.
        chunks = OUT / "chunks"
        chunks.mkdir(exist_ok=True)
        for offset in range(0, len(IDS), BATCH_SIZE):
            query = f'[out:json][timeout:25][maxsize:33554432][date:"{day}T20:59:59Z"];'
            query += (
                "relation(id:"
                + ",".join(map(str, IDS[offset : offset + BATCH_SIZE]))
                + ");out body;>;out body;"
            )
            part = chunks / f"{day}-{BATCH_SIZE}-{offset}.json"
            if not part.exists():
                data = fetch(ENDPOINT + "?" + urlencode({"data": query}))
                part.write_text(
                    json.dumps({"query": query, "endpoint": ENDPOINT, "data": data}, ensure_ascii=False)
                )
            chunk = json.loads(part.read_text())
            queries.append({"query": chunk["query"], "endpoint": chunk["endpoint"]})
            for element in chunk["data"]["elements"]:
                key = (element["type"], element["id"])
                if key in elements and elements[key] != element:
                    raise ValueError(f"Inconsistent OSM object in {day}: {key}")
                elements[key] = element
            print(f"Batch {day} {offset // BATCH_SIZE + 1}", flush=True)
        data = {"elements": list(elements.values())}
        document = {
            "asOf": day,
            "retrievedAt": datetime.now(timezone.utc).isoformat(),
            "endpoint": ENDPOINT,
            "queries": queries,
            "data": data,
        }
        path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
        print(f"Extract {day}: {len(data['elements'])} elements", flush=True)
    (OUT / "index.json").write_text(
        json.dumps(
            {
                "start": "2025-01-01",
                "end": "2026-01-01",
                "dates": sorted(days),
                "basis": "osm_relation_edits",
                "sampling": "end_of_moscow_day",
                "limitation": "Даты правок OSM не гарантируют даты изменений движения. Временные изменения могут отсутствовать.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
