"""Install the bundled model against identical data, verify its CSV, then publish."""

import json
import zipfile
from pathlib import Path

from ..data.repository import DatasetRepository
from ..domain import DomainError
from ..storage import SnapshotStore, file_hash, write_json
from .baseline_features import history_fingerprint
from .contracts import ForecastSpec, TrainingSpec
from .packages import ModelPackageService
from .service import ModelService


def install_baseline(settings, package, *, publish=False, bundled=False):
    package = Path(package)
    store, data = SnapshotStore(settings.state_dir), DatasetRepository(settings.data_root)
    checksum = file_hash(package)
    receipt = store.root / "baseline-install.json"
    with store.lock("baseline-install"):
        if receipt.is_file() and not publish:
            previous = json.loads(receipt.read_bytes())
            if (
                previous["package_sha256"] == checksum
                and store.path("trained_models", previous["model_id"], "joblib").is_file()
            ):
                return previous
        with zipfile.ZipFile(package) as archive:
            meta = json.loads(archive.read("manifest.json"))
        training = TrainingSpec.model_validate(meta["manifest"]["spec"])
        if training.model_type != "competition_catboost":
            raise DomainError("INVALID_MODEL_PACKAGE", "Требуется пакет конкурсной базы CatBoost")
        match = None
        for dataset in data.catalog.datasets():
            if set(training.route_ids) - set(dataset["routes"]):
                continue
            history = data.frame(
                dataset["id"],
                route_ids=training.route_ids,
                start=training.time_range.start,
                end=training.time_range.end,
            )
            if history_fingerprint(history) == meta["parameters"]["history_sha256"]:
                match = dataset["id"]
                break
        if match is None:
            raise DomainError(
                "BASELINE_DATA_REQUIRED", "Импортируйте полную официальную историю января–октября 2025 года"
            )
        models = ModelService(data, store, settings.model_threads, settings.max_forecast_hours)
        model = ModelPackageService(models).import_package(package, dataset_id=match)
        evidence = model["competition_result"]
        forecast = models.predict(
            ForecastSpec(
                model_id=model["id"],
                dataset_id=match,
                route_ids=training.route_ids,
                origin=evidence["origin"],
                time_range=evidence["time_range"],
            )
        )
        from ..platform_exports import PeriodExporter, PeriodExportSpec

        exported = PeriodExporter(settings, data, models).write(
            PeriodExportSpec(
                dataset_id=match,
                forecast_id=forecast["id"],
                route_ids=training.route_ids,
                time_range=evidence["time_range"],
                mode="forecast",
                format="competition",
            ),
            forecast["id"],
        )
        if exported["sha256"] != evidence["submission_sha256"]:
            raise DomainError(
                "BASELINE_PARITY_FAILED", "Экспорт приложения не совпадает с конкурсным submission.csv"
            )
        current = store.current()
        demo_path = store.root / "demo.json"
        demo = json.loads(demo_path.read_bytes()) if demo_path.exists() else {}
        # A bundled upgrade may replace the original demo, but never a user's later publication.
        demo_default = current.get("forecastId") in demo.get("forecasts", [])
        should_publish = publish or (bundled and not receipt.exists() and demo_default)
        result = {
            "package_sha256": checksum,
            "model_id": model["id"],
            "forecast_id": forecast["id"],
            "dataset_id": match,
            "submission_sha256": exported["sha256"],
            "previous_snapshot_id": current.get("snapshotId"),
            "published": False,
        }
        if should_publish:
            from ..platform_api import PublishRequest
            from ..publication import publish_forecast

            published = publish_forecast(
                settings,
                data,
                models,
                PublishRequest(
                    forecast_id=forecast["id"],
                    schedule_id=current.get("scheduleId"),
                    schedule_scenario=current.get("scheduleScenario", False),
                    expected_snapshot_id=current.get("snapshotId"),
                ),
            )
            result.update(published=True, snapshot_id=published["snapshotId"])
        write_json(receipt, result)
        return result
