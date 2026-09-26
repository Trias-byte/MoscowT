from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from moscowt.api import create_app
from moscowt.config import Settings
from moscowt.domain import DomainError
from moscowt.network_history import resolve_network
from moscowt.route_imports import COLUMNS, RouteImportRequest, apply_import, parse_route_csv, preview_import
from moscowt.storage import SnapshotStore


def csv_text(route="99", start="2025-07-01", end="2025-08-01"):
    prefix = f"{route};Тестовый маршрут;0;{start};{end};"
    return (
        ";".join(COLUMNS)
        + "\n"
        + "\n".join(
            [
                prefix + "1;37.620;55.750;a;Первая",
                prefix + "2;37.621;55.751;;",
                prefix + "3;37.622;55.752;b;Вторая",
            ]
        )
    )


@pytest.fixture
def archive_store(tmp_path, store):
    # A separate store per test prevents CSV publication from affecting the shared model fixture.
    local = SnapshotStore(tmp_path)
    base = deepcopy(store.read("networks", store.current()["networkId"]))
    if base.get("versions"):
        base = deepcopy(store.read("networks", base["versions"][0]["networkId"]))
    base["networkSnapshotId"] = "network-base"
    local.put("networks", "network-base", base)
    archive = deepcopy(base)
    archive.update(
        networkSnapshotId="network-archive",
        versions=[{"validFrom": "2025-01-01", "validTo": "2026-01-01", "networkId": "network-base"}],
        historyRange={"start": "2025-01-01", "end": "2026-01-01"},
        historyBasis="osm_relation_edits",
        imports=[],
    )
    local.put("networks", "network-archive", archive)
    local.publish(networkId="network-archive", historyId="history-preserved", forecastId="forecast-preserved")
    return local


def test_csv_preview_is_non_publishing_and_apply_survives_restart_with_exact_dates(archive_store, dataset):
    store = archive_store
    before = store.current()
    with TestClient(
        create_app(Settings(state_dir=store.root, dataset_dir=dataset, worker_enabled=False))
    ) as client:
        response = client.post(
            "/api/v1/route-imports/preview",
            json={"snapshotId": before["snapshotId"], "filename": "route.csv", "csv": csv_text()},
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["stopCount"] == 2 and preview["pointCount"] == 3
        assert not preview["route"]["hasData"]
        assert store.current() == before
        response = client.post(
            f"/api/v1/route-imports/{preview['id']}/apply", json={"snapshotId": before["snapshotId"]}
        )
        assert response.status_code == 200, response.text
        assert (
            client.post(
                f"/api/v1/route-imports/{preview['id']}/apply", json={"snapshotId": before["snapshotId"]}
            ).json()
            == response.json()
        )
    restarted = SnapshotStore(store.root)
    current = restarted.current()
    assert current["historyId"] == before["historyId"] and current["forecastId"] == before["forecastId"]
    archive = restarted.read("networks", current["networkId"])
    for day, present in (
        ("2025-06-30", False),
        ("2025-07-01", True),
        ("2025-07-31", True),
        ("2025-08-01", False),
    ):
        network = resolve_network(restarted, archive, day)
        assert any(p["routeId"] == "99" for p in network["patterns"]) == present
    assert all(
        p["routeId"] != "99"
        for p in resolve_network(restarted, restarted.read("networks", before["networkId"]), "2025-07-01")[
            "patterns"
        ]
    )


def test_replacement_only_overrides_selected_direction_and_reverts_after_end(archive_store):
    store = archive_store
    before = store.current()
    original = resolve_network(store, store.read("networks", before["networkId"]), "2025-07-01")
    preview = preview_import(
        store, RouteImportRequest(snapshotId=before["snapshotId"], filename="one.csv", csv=csv_text("1"))
    )
    result = apply_import(store, preview["id"], before["snapshotId"])
    archive = store.read("networks", result["networkSnapshotId"])
    during = resolve_network(store, archive, "2025-07-01")
    ids = {p["id"] for p in during["patterns"] if p["routeId"] == "1"}
    unchanged = next(p["id"] for p in original["patterns"] if p["routeId"] == "1" and p["direction"] == 1)
    assert len(ids) == 2 and unchanged in ids
    assert any(p.get("sourceKind") == "user_csv" for p in during["patterns"] if p["routeId"] == "1")
    after = resolve_network(store, archive, "2025-08-01")
    assert {p["id"] for p in after["patterns"] if p["routeId"] == "1"} == {
        p["id"] for p in original["patterns"] if p["routeId"] == "1"
    }


def test_concurrent_import_from_stale_network_is_rejected(archive_store):
    store = archive_store
    old = store.current()["snapshotId"]
    first = preview_import(store, RouteImportRequest(snapshotId=old, filename="one.csv", csv=csv_text("99")))
    second = preview_import(store, RouteImportRequest(snapshotId=old, filename="two.csv", csv=csv_text("98")))
    apply_import(store, first["id"], old)
    with pytest.raises(DomainError, match="Сеть изменилась"):
        apply_import(store, second["id"], old)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.replace("2025-07-01", "2026-07-01"),
        lambda s: s.replace(";2;37.621", ";1;37.621"),
        lambda s: s.replace("37.621", "NaN"),
        lambda s: s.replace("37.621", "37.8"),
        lambda s: s.replace(";a;Первая", ";;"),
        lambda s: s.replace(";b;Вторая", ";b;"),
        lambda s: s.replace(";stop_name", ""),
        lambda s: s.replace("99;Тестовый", "99;Тестовый", 1) + "\ninvalid",
    ],
)
def test_invalid_route_csv_rejects_without_publishing(mutate):
    with pytest.raises(DomainError):
        parse_route_csv(mutate(csv_text()), "bad.csv")


def test_bom_quoted_names_and_comma_delimiter():
    text = "\ufeff" + csv_text().replace(";Тестовый маршрут;", ';"Тест; с кавычкой """;')
    assert parse_route_csv(text, "route.csv")["route"]["name"] == 'Тест; с кавычкой "'
    assert parse_route_csv(csv_text().replace(";", ","), "route.csv")["pointCount"] == 3
