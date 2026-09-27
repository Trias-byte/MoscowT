"""Application services used by persisted queue jobs."""

from .config import Settings
from .constants.tasks import MODEL_KINDS
from .data.repository import DatasetRepository
from .data.schemas import ImportSpec
from .modeling.contracts import ForecastSpec, TrainingSpec
from .modeling.packages import EnsembleWeights, ModelPackageService
from .modeling.service import ModelService
from .schedules import ScheduleService
from .storage import SnapshotStore, atomic_write


class JobExecutor:
    def __init__(self, settings: Settings, job: dict):
        self.settings, self.job = settings, job
        self.payload = job["payload"]
        self.store = SnapshotStore(settings.state_dir)
        self.data = DatasetRepository(
            settings.data_root, read_only=job["kind"] in MODEL_KINDS and job["kind"] != "model_refresh"
        )
        self.models = ModelService(self.data, self.store, settings.model_threads, settings.max_forecast_hours)

    def execute(self):
        handlers = {
            "model_refresh": self.refresh_model,
            "scenario_run": self.run_scenario,
            "model_import": self.import_model,
            "model_reweight": self.reweight_model,
            "model_export": self.export_model,
            "factor_fetch": self.fetch_factors,
            "weather_fetch": self.fetch_weather,
            "train": self.train,
            "forecast": self.forecast,
            "import_preview": self.preview_import,
            "import_apply": self.apply_import,
            "schedule_import": self.import_schedule,
            "period_export": self.export_period,
            "bundle_export": self.export_bundle,
        }
        handler = handlers.get(self.job["kind"])
        if handler is None:
            raise ValueError(f"Unknown work kind: {self.job['kind']}")
        return handler()

    def refresh_model(self):
        from .modeling.refresh import ModelRefresh

        return ModelRefresh(self.settings, self.data, self.models, self.job).run()

    def run_scenario(self):
        from .scenario_engine import ScenarioForecastService

        return ScenarioForecastService(self.settings, self.data, self.models).run(
            self.payload["scenario_id"], self.job["id"]
        )

    def import_model(self):
        return ModelPackageService(self.models).import_package(
            self.store.path("uploads", self.payload["blob_id"], "zip")
        )

    def reweight_model(self):
        model = ModelPackageService(self.models).reweight(
            self.payload["model_id"], EnsembleWeights.model_validate(self.payload["weights"])
        )
        if self.payload.get("forecast_id"):
            base = self.store.read("forecast_runs", self.payload["forecast_id"])
            forecast = self.models.predict(
                ForecastSpec.model_validate({**base["spec"], "model_id": model["id"]})
            )
            return {**model, "forecast_id": forecast["id"]}
        return model

    def export_model(self):
        content = ModelPackageService(self.models).export(self.payload["model_id"])
        filename = self.job["id"] + ".zip"
        atomic_write(self.store.root / "exports" / filename, content)
        return {"filename": filename}

    def fetch_factors(self):
        from .factors import FactorRepository

        repository = FactorRepository(self.settings.data_root)
        if self.payload["kind"] == "weather":
            return repository.fetch_weather(self.payload["start"], self.payload["end"])
        archive = repository.fetch_accidents()
        links = repository.link_accidents(archive["id"], self.store, self.payload["network_id"])
        return {"archive": archive, "links": links}

    def fetch_weather(self):
        from .external import OpenMeteoForecastProvider

        return OpenMeteoForecastProvider(self.settings.data_root).fetch()

    def train(self):
        return self.models.train(TrainingSpec.model_validate(self.payload))

    def forecast(self):
        return self.models.predict(ForecastSpec.model_validate(self.payload))

    def preview_import(self):
        from .modeling.refresh import is_descendant

        source = self.store.path("uploads", self.payload["blob_id"], "csv")
        report = self.data.preview([source], ImportSpec.model_validate(self.payload["spec"]))
        base = report.get("base_dataset_id")
        published = self.store.current().get("datasetId")
        update_default = bool(base and published and is_descendant(self.data, base, published))
        return {**report, "update_forecast_default": update_default}

    def apply_import(self):
        return self.data.apply(self.payload["upload_id"])

    def import_schedule(self):
        return ScheduleService(self.settings.data_root).import_file(
            self.store.path("uploads", self.payload["blob_id"], "csv")
        )

    def export_period(self):
        from .platform_exports import PeriodExporter, PeriodExportSpec

        return PeriodExporter(self.settings, self.data, self.models).write(
            PeriodExportSpec.model_validate(self.payload), self.job["id"]
        )

    def export_bundle(self):
        from .bundles import create_bundle

        return create_bundle(self.settings, self.job["id"])
