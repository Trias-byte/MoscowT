"""Coverage of real values, independent of the padded import selection."""

from functools import lru_cache
from pathlib import Path

from ..data.repository import DatasetRepository


def summarize_history(frame):
    routes = {}
    for route, rows in frame.groupby("route"):
        known = rows.loc[rows.value.notna()]
        routes[route] = {
            "known_hours": len(known),
            "missing_hours": int(rows.value.isna().sum()),
            "first_observation": known.timestamp.min().isoformat() if len(known) else None,
            "last_observation": known.timestamp.max().isoformat() if len(known) else None,
        }
    known = frame.loc[frame.value.notna()]
    from pandas import Timedelta

    return {
        "first_observation": known.timestamp.min().isoformat() if len(known) else None,
        "last_observation": known.timestamp.max().isoformat() if len(known) else None,
        "forecast_origin": (known.timestamp.max() + Timedelta(hours=1)).isoformat() if len(known) else None,
        "route_coverage": routes,
    }


@lru_cache(maxsize=64)
def dataset_coverage(root, ident):
    return summarize_history(DatasetRepository(Path(root), read_only=True).frame(ident))
