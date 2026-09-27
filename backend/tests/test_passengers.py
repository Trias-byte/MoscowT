import io
import json
import shutil
import tarfile
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from moscowt.api import create_app
from moscowt.bundles import create_bundle, restore_bundle
from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.domain import DomainError
from moscowt.passenger_api import PassengerQuery
from moscowt.passenger_package import activate_package, install_package, verify_package
from moscowt.passengers import PassengerRepository
from moscowt.storage import SnapshotStore, canonical, digest

PACKAGE = Path(__file__).resolve().parents[2] / "deliverables/passengers.tar.gz"


@pytest.fixture(scope="module")
def installed_passengers(tmp_path_factory):
    root = tmp_path_factory.mktemp("passenger-release")
    result = install_package(root, PACKAGE)
    return root, result["id"]


@pytest.fixture
def passenger_env(tmp_path, installed_passengers):
    source, ident = installed_passengers
    root = tmp_path / "data"
    shutil.copytree(source, root)
    settings = Settings(state_dir=tmp_path / "state", data_root=root, worker_enabled=False)
    store, data = SnapshotStore(settings.state_dir), DatasetRepository(root)
    repo = PassengerRepository(root, store, data)
    release = repo.current()
    total = release.frame("route_hour_total")
    grid = pd.MultiIndex.from_product(
        [
            release.manifest["routes"],
            pd.date_range("2025-01-01", "2025-11-01", tz="Europe/Moscow", inclusive="left", freq="h"),
        ],
        names=["route", "timestamp"],
    )
    history = (
        total.set_index(["route", "timestamp"])
        .boardings.reindex(grid, fill_value=0)
        .reset_index(name="value")
    )
    store.put("histories", "history-passengers", {"id": "history-passengers"})
    history.to_parquet(store.path("histories", "history-passengers", "parquet"))
    stops = release.frame("stop_infrastructure_monthly")
    versions = []
    for month in release.manifest["snapshots"]:
        rows = stops[stops.snapshot.eq(month)]
        network = {
            "routes": [{"id": route} for route in rows.route.unique()],
            "patterns": [],
            "segments": [],
            "stops": [
                {
                    "id": row.stop_occurrence_id,
                    "stationId": row.stop_id,
                    "routeId": row.route,
                    "patternId": "pattern-" + row.route,
                    "coordinates": [row.longitude, row.latitude],
                }
                for row in rows.itertuples()
            ],
        }
        network["patterns"] = [{"id": "pattern-" + route, "routeId": route} for route in rows.route.unique()]
        network_id = "network-" + month
        store.put("networks", network_id, network)
        versions.append(
            {
                "networkId": network_id,
                "validFrom": month,
                "validTo": str((pd.Timestamp(month) + pd.offsets.MonthBegin()).date()),
            }
        )
    archive = {
        **network,
        "versions": versions,
        "networkSnapshotId": "network-archive",
        "historyRange": {"start": "2025-01-01", "end": "2025-11-01"},
        "historyBasis": "fixture",
    }
    store.put("networks", "network-archive", archive)
    snapshot = store.publish(historyId="history-passengers", networkId="network-archive")
    return settings, repo, snapshot, ident


def spec(snapshot, **changes):
    return PassengerQuery.model_validate(
        {
            "snapshot_id": snapshot["snapshotId"],
            "route_ids": ["1", "7"],
            "time_range": {"start": "2025-09-01T00:00:00+03:00", "end": "2025-09-02T00:00:00+03:00"},
            **changes,
        }
    )


def test_full_release_totals_keys_coordinates_and_no_raw_cards(installed_passengers):
    root, ident = installed_passengers
    m = verify_package(root / "passengers" / ident)
    assert m["total_boardings"] == 59667191
    assert m["unique_cards"] == 4979766
    assert len(m["snapshots"]) == 10
    assert not any(name.startswith(("card_", "events", "forecast/features", "model_")) for name in m["files"])
    total = pd.read_parquet(root / "passengers" / ident / "route_hour_total.parquet")
    cats = pd.read_parquet(root / "passengers" / ident / "route_hour_category.parquet")
    assert len(total) == 57551
    keys = ["route", "timestamp"]
    assert (
        cats.groupby(keys).boardings.sum().sort_index().equals(total.set_index(keys).boardings.sort_index())
    )
    for month in m["snapshots"]:
        poi = pd.read_parquet(root / "passengers" / ident / f"poi_{month}.parquet")
        assert poi.longitude.between(36, 39).all() and poi.latitude.between(54, 57).all()
        assert "geometry" not in poi


def test_queries_reconcile_timezone_filter_denominator_and_unique_cards(passenger_env):
    _, repo, snapshot, _ = passenger_env
    all_rows = repo.query(spec(snapshot))
    assert all_rows["meta"]["status"] == "ready"
    assert sum(row["boardings"] for row in all_rows["summary"]) == all_rows["total"]
    assert len(all_rows["rows"]) == 2 * 24 * 6
    assert all_rows["rows"][0]["timestamp"] == "2025-09-01T00:00:00+03:00"
    assert all(row["unique_cards"] is None for row in all_rows["summary"])
    social = repo.query(spec(snapshot, categories=["social"]))
    assert social["total"] == all_rows["total"]
    assert social["summary"][0]["share"] == pytest.approx(
        social["summary"][0]["boardings"] / all_rows["total"]
    )
    daily = repo.query(spec(snapshot, grain="day"))
    assert daily["total"] == all_rows["total"]
    assert all(row["unique_cards"] is None for row in daily["rows"])
    hour = repo.query(
        spec(
            snapshot,
            route_ids=["1"],
            time_range={"start": "2025-09-01T05:00:00Z", "end": "2025-09-01T06:00:00Z"},
        )
    )
    source = repo.current().frame("route_hour_total")
    expected = source[(source.route == "1") & (source.timestamp == pd.Timestamp("2025-09-01T08:00:00+03:00"))]
    assert hour["total"] == expected.boardings.iloc[0]
    assert all(row["unique_cards"] is not None for row in hour["summary"])


def test_unavailable_and_mismatched_history_never_fabricates_categories(passenger_env):
    _, repo, snapshot, _ = passenger_env
    for changes in (
        {"route_ids": []},
        {"route_ids": ["new"]},
        {"mode": "forecast"},
        {"time_range": {"start": "2025-10-31T23:00:00+03:00", "end": "2025-11-01T01:00:00+03:00"}},
    ):
        result = repo.query(spec(snapshot, **changes))
        assert result["meta"]["status"] == "unavailable" and result["rows"] == [] and result["total"] is None
    path = repo.store.path("histories", "history-passengers", "parquet")
    frame = pd.read_parquet(path)
    frame.loc[
        (frame.route == "1") & frame.timestamp.eq(pd.Timestamp("2025-09-01T08:00:00+03:00")), "value"
    ] += 1
    frame.to_parquet(path)
    assert repo.query(spec(snapshot))["meta"]["status"] == "incompatible"


def test_all_months_spatial_deduplication_and_radius(passenger_env):
    _, repo, snapshot, _ = passenger_env
    for month in repo.current().manifest["snapshots"]:
        args = (snapshot["snapshotId"], month, "1")
        infra = repo.infrastructure(*args, 500)
        assert infra["meta"]["status"] == "ready"
        count = next(x["value"] for x in infra["rows"] if x["kind"] == "school")
        small = repo.poi(snapshot["snapshotId"], month, ["1"], ["school"], 500, None)
        large = repo.poi(snapshot["snapshotId"], month, ["1"], ["school"], 1000, None)
        assert len(small["features"]) == count
        assert len({f["id"] for f in small["features"]}) == len(small["features"])
        assert {f["id"] for f in small["features"]} <= {f["id"] for f in large["features"]}
        assert not repo.infrastructure(snapshot["snapshotId"], month, "5", 500)["rows"]
    assert not repo.poi(snapshot["snapshotId"], "2026-01-01", ["1"], [], 500, None)["features"]
    assert not repo.poi(snapshot["snapshotId"], "2025-09-01", ["1"], [], 500, [0, 0, 1, 1])["features"]


def test_changed_stop_and_imported_route_are_not_enriched(passenger_env):
    _, repo, snapshot, _ = passenger_env
    network = repo.network(snapshot["snapshotId"], "2025-09-01")
    stop = next(s for s in network["stops"] if s["routeId"] == "1")
    result = repo.infrastructure(snapshot["snapshotId"], "2025-09-01", "1", 500, stop["id"])
    assert result["meta"]["status"] == "ready"
    stop["coordinates"] = [0, 0]
    repo.store.put("networks", "network-imported", network)
    changed = repo.store.create_snapshot(snapshot, networkId="network-imported")
    assert (
        repo.infrastructure(changed["snapshotId"], "2025-09-01", "1", 500, stop["id"])["meta"]["status"]
        == "incompatible"
    )
    assert not repo.poi(changed["snapshotId"], "2025-09-01", ["1"], [], 500, None)["features"]


def test_api_metadata_fixed_cohorts_validation_and_missing_package(passenger_env, tmp_path):
    settings, repo, snapshot, _ = passenger_env
    with TestClient(create_app(settings)) as client:
        prefix = "/api/v2/passengers"
        assert client.get(prefix + "/metadata").json()["available"]
        result = client.post(prefix + "/query", json=spec(snapshot).model_dump(mode="json"))
        assert result.status_code == 200
        assert (
            client.get(prefix + "/seasonality?route_ids=1").json()["meta"]["filters"]["profiles_scope"]
            == "network"
        )
        assert client.get(prefix + "/cohorts").json()["meta"]["filters"]["scope"] == "network"
        params = {"snapshot_id": snapshot["snapshotId"], "date": "2025-09-01", "route": "1", "radius": "500"}
        assert client.get(prefix + "/infrastructure", params=params).status_code == 200
        poi = client.get(
            prefix + "/poi",
            params={
                "snapshot_id": snapshot["snapshotId"],
                "date": "2025-09-01",
                "route_ids": "1",
                "categories": "school",
                "radius": "500",
            },
        )
        assert poi.status_code == 200 and poi.json()["features"]
        assert poi.json()["meta"]["reason"] is None
        future = client.get(prefix + "/infrastructure", params={**params, "date": "2026-01-01"})
        assert future.status_code == 200 and future.json()["rows"] == []
        contracts = client.get("/openapi.json").json()
        assert (
            "$ref"
            in contracts["paths"][prefix + "/query"]["post"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
        )
        params["radius"] = "700"
        assert client.get(prefix + "/infrastructure", params=params).status_code == 422
        assert (
            client.get(
                prefix + "/poi", params={"snapshot_id": snapshot["snapshotId"], "date": "bad"}
            ).status_code
            == 422
        )
        bad = spec(snapshot).model_dump(mode="json")
        bad["categories"] = ["invented_age"]
        assert client.post(prefix + "/query", json=bad).status_code == 422
    path = repo.current().folder / "route_hour_total.parquet"
    path.write_bytes(b"corrupted")
    assert repo.metadata()["status"] == "corrupt"
    empty = PassengerRepository(tmp_path / "missing", repo.store, repo.data)
    assert empty.metadata()["available"] is False


def repackage(source, output, change):
    with tarfile.open(source, "r:gz") as src, tarfile.open(output, "w:gz") as dst:
        for member in src.getmembers():
            content = src.extractfile(member).read()
            if member.name == "manifest.json":
                value = json.loads(content)
                change(value)
                value["id"] = "passengers-" + digest({k: v for k, v in value.items() if k != "id"})
                content = canonical(value)
            info = tarfile.TarInfo(member.name)
            info.size = len(content)
            dst.addfile(info, io.BytesIO(content))


def test_install_upgrade_rollback_restart_and_rejected_archive(tmp_path):
    root = tmp_path / "data"
    first = install_package(root, PACKAGE, bundled=True)["id"]
    upgrade = tmp_path / "upgrade.tar.gz"
    repackage(PACKAGE, upgrade, lambda m: m.update(taxonomy_version="1.1"))
    second = install_package(root, upgrade, bundled=True)["id"]
    assert first != second
    assert (root / "passengers" / first).is_dir()
    activate_package(root, first)
    install_package(root, upgrade, bundled=True)
    assert json.loads((root / "passengers/current.json").read_bytes())["id"] == first
    # A mismatched checksum must never change the active pointer.
    broken = tmp_path / "broken.tar.gz"
    repackage(PACKAGE, broken, lambda m: m["files"]["route_hour_total.parquet"].update(sha256="0" * 64))
    with pytest.raises(DomainError):
        install_package(root, broken)
    assert json.loads((root / "passengers/current.json").read_bytes())["id"] == first
    assert not list((root / "passengers").glob(".install-*"))


def test_portable_bundle_contains_passengers_and_preserves_pointer(passenger_env, tmp_path):
    settings, _, _, ident = passenger_env
    bundle = create_bundle(settings, "passenger-portable")
    restored = Settings(
        state_dir=tmp_path / "restored-state", data_root=tmp_path / "restored-data", worker_enabled=False
    )
    restore_bundle(settings.state_dir / "exports" / bundle["filename"], restored)
    assert json.loads((restored.data_root / "passengers/current.json").read_bytes())["id"] == ident
    assert verify_package(restored.data_root / "passengers" / ident)["total_boardings"] == 59667191


def test_failed_bundled_update_keeps_core_and_previous_release(passenger_env, tmp_path, monkeypatch, caplog):
    from moscowt.entrypoint import install_passengers

    settings, repo, _, ident = passenger_env
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"invalid archive")
    monkeypatch.setenv("MOSCOWT_PASSENGER_PACKAGE", str(broken))
    install_passengers(settings)
    assert repo.current().manifest["id"] == ident
    assert "previous pointer retained" in caplog.text
