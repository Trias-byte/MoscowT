"""Download a reproducible, dated Moscow tram extract (never called by the web app)."""

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--date", type=date.fromisoformat, default=date(2025, 1, 1))
parser.add_argument("--endpoint", default="https://maps.mail.ru/osm/tools/overpass/api/interpreter")
parser.add_argument("--output", type=Path)
args = parser.parse_args()
refs = {"1", "2", "3", "4", "5", "6", "7", "10", "11", "12", "17", "25", "26", "28", "50"}
prefix = f'[out:json][timeout:90][date:"{args.date.isoformat()}T00:00:00Z"];'


def fetch(query):
    request = Request(
        args.endpoint + "?" + urlencode({"data": query}), headers={"User-Agent": "MoscowT-local-map/1.0"}
    )
    with urlopen(request, timeout=110) as response:
        data = json.load(response)
    if data.get("remark"):
        raise ValueError(data["remark"])
    return data


discovery = prefix + 'relation["route"="tram"](55.45,37.25,55.95,37.95);out tags;'
relations = [e for e in fetch(discovery)["elements"] if e.get("tags", {}).get("ref") in refs]
if not relations:
    raise ValueError("No tram routes at this date")
query = prefix + "relation(id:" + ",".join(str(e["id"]) for e in relations) + ");out body;>;out body;"
document = {
    "asOf": args.date.isoformat(),
    "retrievedAt": datetime.now(timezone.utc).isoformat(),
    "endpoint": args.endpoint,
    "query": query,
    "data": fetch(query),
}
output = (
    args.output
    or Path(__file__).resolve().parents[2] / "dataset/geography" / f"moscow-trams-{args.date}.json"
)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
print(f"Saved {len(relations)} route directions to {output}")
