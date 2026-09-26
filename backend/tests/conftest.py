from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moscowt.api import create_app
from moscowt.config import Settings
from moscowt.domain import HISTORY_END
from moscowt.fleet import prepare_fleet
from moscowt.forecasting import fit_model, issue_forecast
from moscowt.pipelines import prepare_labels, prepare_network
from moscowt.storage import SnapshotStore

DATASET = Path(__file__).resolve().parents[2] / "dataset"


@pytest.fixture(scope="session")
def dataset():
    return DATASET


@pytest.fixture(scope="session")
def store(tmp_path_factory):
    store = SnapshotStore(tmp_path_factory.mktemp("artifacts"))
    history = prepare_labels(store, DATASET)
    prepare_fleet(store, DATASET)
    prepare_network(store, DATASET)
    model = fit_model(store, history["id"], HISTORY_END, "seasonal")
    issue_forecast(store, model["id"])
    return store


@pytest.fixture
def scope(store):
    return {
        "routeIds": ["1", "5", "7", "11", "12", "17", "25", "26", "28", "50"],
        "snapshotId": store.current()["snapshotId"],
        "timeRange": {"start": "2025-11-01T00:00:00+03:00", "end": "2025-11-02T00:00:00+03:00"},
    }


@pytest.fixture
def client(store, dataset):
    with TestClient(
        create_app(Settings(state_dir=store.root, dataset_dir=dataset, worker_enabled=False))
    ) as c:
        yield c
