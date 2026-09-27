import pandas as pd
import pytest

from moscowt.data.catalog import RouteDefinition
from moscowt.data.normalization import SourceNormalizer
from moscowt.data.repository import DatasetRepository
from moscowt.data.schemas import ImportSpec
from moscowt.domain import DomainError


def labels(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("route;date;hour;boardings\n" + "\n".join(rows) + "\n")
    return path


def spec(start="2025-01-01", end="2025-01-02", **kwargs):
    return ImportSpec(
        time_range={"start": start + "T00:00:00+03:00", "end": end + "T00:00:00+03:00"}, **kwargs
    )


def ingest(repository, path, selection):
    preview = repository.preview([path], selection)
    return repository.apply(preview["id"])


def test_dataset_versions_are_shared_idempotent_and_do_not_invent_zeros(tmp_path):
    root = tmp_path / "shared"
    repo = DatasetRepository(root)
    source = labels(tmp_path, "january.csv", ["101;2025-01-01;8;12"])
    first = ingest(repo, source, spec())
    frame = DatasetRepository(root).frame(first["dataset_id"])
    assert len(frame) == 24
    assert frame.value.isna().sum() == 23
    assert frame.value.sum() == 12
    assert repo.catalog.routes()[0]["id"] == "101"
    duplicate = ingest(repo, source, spec())
    assert duplicate["duplicate"] is True
    assert duplicate["dataset_id"] == first["dataset_id"]
    assert len(repo.catalog.datasets()) == 1
    assert repo.verify(first["dataset_id"])["verified"]


def test_new_month_and_new_route_expand_without_changing_old_snapshot(tmp_path):
    repo = DatasetRepository(tmp_path / "shared")
    first = ingest(repo, labels(tmp_path, "one.csv", ["1;2025-01-01;8;10"]), spec(complete=True))
    second = ingest(
        repo,
        labels(tmp_path, "two.csv", ["new-line;2026-02-01;8;20"]),
        spec("2026-02-01", "2026-02-02", complete=True),
    )
    assert repo.frame(first["dataset_id"]).value.sum() == 10
    assert repo.frame(second["dataset_id"]).value.sum() == 30
    assert repo.manifest(second["dataset_id"])["routes"] == ["1", "new-line"]
    selected = repo.frame(
        second["dataset_id"], route_ids=["new-line"], start=pd.Timestamp("2026-02-01T00:00:00+03:00")
    )
    assert len(selected) == 24 and selected.value.sum() == 20


def test_conflicts_require_explicit_correction_and_partial_import_keeps_known_hours(tmp_path):
    repo = DatasetRepository(tmp_path / "shared")
    original = labels(tmp_path, "old.csv", ["1;2025-01-01;8;10", "1;2025-01-01;9;20"])
    first = ingest(repo, original, spec())
    update = labels(tmp_path, "new.csv", ["1;2025-01-01;8;15"])
    preview = repo.preview([update], spec())
    assert preview["conflict_keys"] == 1
    with pytest.raises(DomainError, match="исправления"):
        repo.apply(preview["id"])
    assert repo.catalog.current_id() == first["dataset_id"]
    fixed = ingest(repo, update, spec(mode="upsert"))
    assert repo.frame(fixed["dataset_id"]).value.sum() == 35
    replaced = ingest(repo, update, spec(mode="replace", route_ids=["1"]))
    assert repo.frame(replaced["dataset_id"]).value.sum() == 15
    assert repo.frame(replaced["dataset_id"]).value.isna().sum() == 23
    assert repo.frame(first["dataset_id"]).value.sum() == 30


def test_two_previews_cannot_overwrite_each_others_publication(tmp_path):
    repo = DatasetRepository(tmp_path / "shared")
    a = repo.preview([labels(tmp_path, "a.csv", ["1;2025-01-01;8;10"])], spec())
    b = repo.preview([labels(tmp_path, "b.csv", ["2;2025-01-01;8;20"])], spec())
    repo.apply(a["id"])
    with pytest.raises(DomainError) as error:
        repo.apply(b["id"])
    assert error.value.code == "DATASET_CHANGED"
    assert len(repo.catalog.datasets()) == 1


def test_route_lifetime_controls_zero_fill_and_route_updates_invalidate_preview(tmp_path):
    repo = DatasetRepository(tmp_path / "shared")
    route = RouteDefinition(id="42", number="42", name="Новый", valid_from="2025-01-02")
    old = repo.catalog.save_route(route)
    source = labels(tmp_path, "new.csv", ["42;2025-01-02;8;10"])
    preview = repo.preview([source], spec(end="2025-01-03", complete=True))
    assert preview["rows"] == 24
    repo.catalog.save_route(route.model_copy(update={"name": "Новое название"}), old["version"])
    with pytest.raises(DomainError) as error:
        repo.apply(preview["id"])
    assert error.value.code == "ROUTE_CHANGED"
    invalid = labels(tmp_path, "before.csv", ["42;2025-01-01;8;10"])
    with pytest.raises(DomainError) as error:
        repo.preview([invalid], spec())
    assert error.value.code == "OUTSIDE_ROUTE_LIFETIME"


def test_streamed_raw_keeps_graph_and_vehicle_and_avoids_duplicate_files(tmp_path):
    repo = DatasetRepository(tmp_path / "shared", SourceNormalizer(chunk_size=1))
    source = tmp_path / "events.csv"
    source.write_text(
        "ngpt_route;tran_date_time;validation_result;bus_exit_no;garage_number\n"
        "101 трамвай;01.01.2025 08:05:00;1;0206;00042\n"
        "101 трамвай;01.01.2025 08:15:00;2;0206;00042\n"
        "101 трамвай;01.01.2025 09:05:00;1;0206;00043\n"
    )
    selection = spec(kind="events")
    preview = repo.preview([source, source], selection)
    assert preview["input_rows"] == 3
    result = repo.apply(preview["id"])
    assert repo.frame(result["dataset_id"]).value.sum() == 2
    events = pd.concat(repo.event_batches(result["dataset_id"]))
    assert len(events) == 3
    assert set(events.bus_exit_no) == {"0206"}
    assert set(events.garage_number) == {"00042", "00043"}
    corrected = ingest(repo, labels(tmp_path, "labels.csv", ["101;2025-01-01;8;12"]), spec(mode="upsert"))
    surviving = pd.concat(repo.event_batches(corrected["dataset_id"]))
    assert len(surviving) == 1 and surviving.iloc[0].timestamp.hour == 9


def test_invalid_labels_do_not_publish_and_partition_corruption_is_detected(tmp_path):
    repo = DatasetRepository(tmp_path / "shared")
    source = labels(tmp_path, "bad.csv", ["1;2025-01-01;8;1", "1;2025-01-01;8;2"])
    with pytest.raises(DomainError) as error:
        repo.preview([source], spec())
    assert error.value.code == "DUPLICATE_LABEL_KEY"
    assert repo.catalog.current_id() is None
    good = ingest(repo, labels(tmp_path, "good.csv", ["1;2025-01-01;8;10"]), spec())
    part = repo.manifest(good["dataset_id"])["partitions"][0]
    repo.files.path("partitions", part["id"], "parquet").write_bytes(b"broken")
    with pytest.raises(DomainError) as error:
        repo.verify(good["dataset_id"])
    assert error.value.code == "DATASET_CORRUPTED"
