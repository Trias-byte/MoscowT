from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from moscowt.api import create_app
from moscowt.config import Settings
from moscowt.data.repository import DatasetRepository
from moscowt.data.schemas import ImportSpec
from moscowt.domain import HISTORY_END
from moscowt.fleet import prepare_fleet
from moscowt.forecasting import fit_model, issue_forecast
from moscowt.pipelines import prepare_labels, prepare_network
from moscowt.storage import SnapshotStore

DATASET = Path(__file__).resolve().parents[2] / "dataset"


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    directory = tmp_path_factory.mktemp("inputs")
    source = Settings().source_dir
    for item in DATASET.iterdir():
        (directory / item.name).symlink_to(item, target_is_directory=item.is_dir())
    for name in ("labels", "test_submission.csv", "spravochniki"):
        target = directory / name
        if not target.exists() and (source / name).exists():
            target.unlink(missing_ok=True)
            target.symlink_to(source / name, target_is_directory=(source / name).is_dir())
    return directory


@pytest.fixture(scope="session")
def store(tmp_path_factory, dataset):
    store = SnapshotStore(tmp_path_factory.mktemp("artifacts"))
    history = prepare_labels(store, dataset)
    prepare_fleet(store, dataset)
    prepare_network(store, dataset)
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


@pytest.fixture
def model_data(tmp_path):
    data = DatasetRepository(tmp_path / "data")
    times = pd.date_range("2026-01-01", "2026-03-01", freq="h", inclusive="left", tz="Europe/Moscow")
    lines = ["route;date;hour;boardings"]
    for route in ("5", "new-101"):
        lines.extend(f"{route};{t.date()};{t.hour};{10 + t.hour + t.dayofweek * 3}" for t in times)
    spec = ImportSpec(time_range={"start": times[0], "end": "2026-03-01T00:00:00+03:00"})
    source = tmp_path / "history.csv"
    source.write_text("\n".join(lines))
    report = data.preview([source], spec)
    ident = data.apply(report["id"])["dataset_id"]
    return data, ident, SnapshotStore(tmp_path / "state")
