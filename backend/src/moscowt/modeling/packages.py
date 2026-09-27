"""Portable, data-only model packages. Never deserialize user-provided pickle in HTTP workers."""

import hashlib
import io
import json
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from pydantic import Field, model_validator

from ..domain import TZ, DomainError, StrictModel
from ..storage import canonical, digest, file_hash
from .contracts import TrainingSpec
from .features import FeatureBuilder

# Required by RandomForestRegressor with sklearn 1.9 / skops 0.14. Never trust types from the file itself.
RF_TRUSTED_TYPES = ["sklearn.tree._tree.Tree"]


class EnsembleWeights(StrictModel):
    catboost: float = Field(default=0.7, ge=0, le=1)
    lightgbm: float = Field(default=0.1, ge=0, le=1)
    random_forest: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def total(self):
        if abs(self.catboost + self.lightgbm + self.random_forest - 1) > 1e-9:
            raise ValueError("Сумма весов должна быть равна 100%")
        return self


class ModelPackageService:
    def __init__(self, models):
        self.models, self.store = models, models.store

    def export(self, ident):
        manifest, model = self.models.load(ident)
        return self.encode(manifest, model)

    @staticmethod
    def encode(manifest, model):
        spec = TrainingSpec.model_validate(manifest["spec"])
        files, estimators = {}, []
        if spec.model_type == "lgb_cb_rf":
            import skops.io as sio
            from catboost import CatBoostRegressor
            from lightgbm import LGBMRegressor
            from sklearn.ensemble import RandomForestRegressor

            if not isinstance(model, dict) or not isinstance(model.get("builder"), FeatureBuilder):
                raise DomainError(
                    "UNSUPPORTED_LEGACY_MODEL",
                    "Нужен ансамбль сервиса с FeatureBuilder; приложите совместимый адаптер признаков",
                )
            with tempfile.TemporaryDirectory() as temp:
                for weight, estimator in model["models"]:
                    if isinstance(estimator, CatBoostRegressor):
                        name, kind = "catboost.cbm", "catboost"
                        estimator.save_model(str(Path(temp) / name))
                        content = (Path(temp) / name).read_bytes()
                    elif isinstance(estimator, LGBMRegressor) or type(estimator).__name__ == "Booster":
                        name, kind = "lightgbm.txt", "lightgbm"
                        booster = estimator.booster_ if isinstance(estimator, LGBMRegressor) else estimator
                        content = booster.model_to_string().encode()
                    elif isinstance(estimator, RandomForestRegressor):
                        name, kind = "random_forest.skops", "random_forest"
                        content = sio.dumps(estimator)
                    else:
                        raise DomainError(
                            "UNSUPPORTED_ESTIMATOR",
                            "Поддерживаются CatBoost, LightGBM и RandomForestRegressor",
                        )
                    if name in files:
                        raise DomainError("DUPLICATE_ESTIMATOR", "Повтор компонента ансамбля")
                    files[name] = content
                    estimators.append({"file": name, "kind": kind, "weight": weight})
            params = {k: v for k, v in model.items() if k not in ("models", "builder")}
            params["builder"] = {
                "route_ids": sorted(model["builder"].route_codes),
                "external_snapshot_id": model["builder"].external_snapshot_id,
                "feature_groups": model["builder"].feature_groups,
                "weather_hourly_id": getattr(model["builder"], "weather_hourly_id", None),
                "accident_links_id": getattr(model["builder"], "accident_links_id", None),
            }
        else:
            params = model
        metadata = {
            "format": "potok-model-v1",
            "manifest": manifest,
            "parameters": params,
            "estimators": estimators,
            "files": {n: hashlib.sha256(c).hexdigest() for n, c in files.items()},
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical(metadata))
            for name, content in files.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def inspect(self, path):
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 5
                    or len({x.filename for x in entries}) != len(entries)
                    or sum(x.file_size for x in entries) > 1024**3
                ):
                    raise ValueError("Размер или состав пакета не поддерживается")
                meta = json.loads(archive.read("manifest.json"))
                if meta["format"] != "potok-model-v1" or set(archive.namelist()) != {
                    "manifest.json",
                    *meta["files"],
                }:
                    raise ValueError("Неизвестный формат пакета")
                for name, sha in meta["files"].items():
                    if (
                        name not in ("catboost.cbm", "lightgbm.txt", "random_forest.skops")
                        or hashlib.sha256(archive.read(name)).hexdigest() != sha
                    ):
                        raise ValueError("Неверный файл или контрольная сумма")
                spec = TrainingSpec.model_validate(meta["manifest"]["spec"])
                self.models.data.manifest(spec.dataset_id)
                if spec.model_type == "lgb_cb_rf":
                    kinds = [e["kind"] for e in meta["estimators"]]
                    if sorted(kinds) != ["catboost", "lightgbm", "random_forest"]:
                        raise ValueError("Требуются три компонента CB/LGB/RF")
                    filenames = {
                        "catboost": "catboost.cbm",
                        "lightgbm": "lightgbm.txt",
                        "random_forest": "random_forest.skops",
                    }
                    if set(meta["files"]) != set(filenames.values()) or any(
                        e["file"] != filenames[e["kind"]] for e in meta["estimators"]
                    ):
                        raise ValueError("Файлы компонентов не соответствуют типам моделей")
                    EnsembleWeights.model_validate({e["kind"]: e["weight"] for e in meta["estimators"]})
                    builder = meta["parameters"]["builder"]
                    if (
                        sorted(builder["route_ids"]) != sorted(spec.route_ids)
                        or builder.get("feature_groups") != spec.feature_groups
                    ):
                        raise ValueError("Описание признаков не соответствует паспорту")
                    for key in ("weather_hourly_id", "accident_links_id", "external_snapshot_id"):
                        if builder.get(key) != getattr(spec, key):
                            raise ValueError("Внешние источники не соответствуют паспорту")
                    import skops.io as sio

                    if set(sio.get_untrusted_types(data=archive.read("random_forest.skops"))) - set(
                        RF_TRUSTED_TYPES
                    ):
                        raise ValueError("RF содержит типы вне разрешённого набора sklearn")
            return meta
        except (ValueError, KeyError, zipfile.BadZipFile, TypeError) as exc:
            raise DomainError("INVALID_MODEL_PACKAGE", str(exc)) from exc

    def import_package(self, path):
        meta = self.inspect(path)
        spec = TrainingSpec.model_validate(meta["manifest"]["spec"])
        model = dict(meta["parameters"])
        if spec.model_type == "lgb_cb_rf":
            import skops.io as sio
            from catboost import CatBoostRegressor
            from lightgbm import Booster
            from sklearn.ensemble import RandomForestRegressor

            model["builder"] = FeatureBuilder(**model["builder"])
            model["models"] = []
            with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as temp:
                for entry in meta["estimators"]:
                    content = archive.read(entry["file"])
                    if entry["kind"] == "catboost":
                        p = Path(temp) / "model.cbm"
                        p.write_bytes(content)
                        estimator = CatBoostRegressor().load_model(str(p))
                    elif entry["kind"] == "lightgbm":
                        estimator = Booster(model_str=content.decode())
                    else:
                        estimator = sio.loads(content, trusted=RF_TRUSTED_TYPES)
                        if type(estimator) is not RandomForestRegressor:
                            raise DomainError("INVALID_RF", "Ожидается RandomForestRegressor")
                    model["models"].append((entry["weight"], estimator))
        # Build real features and run all components before publishing an imported model.
        history = self.models._history(
            spec.dataset_id, spec.route_ids, spec.time_range.start, spec.time_range.end, 56
        )
        import numpy as np
        import pandas as pd

        from .adapters import ADAPTERS
        from .contracts import ForecastSpec

        sample = ForecastSpec(
            model_id="preview",
            dataset_id=spec.dataset_id,
            route_ids=spec.route_ids,
            origin=spec.time_range.end,
            time_range={"start": spec.time_range.end, "end": spec.time_range.end + pd.Timedelta(hours=1)},
        )
        values = ADAPTERS[spec.model_type].predict(model, history, sample).value
        if not np.isfinite(values).all():
            raise DomainError("INVALID_MODEL_OUTPUT", "Пакет возвращает некорректные значения")
        return self._save(model, meta["manifest"], {"package_sha256": file_hash(path)})

    def reweight(self, ident, weights):
        manifest, model = self.models.load(ident)
        if manifest["spec"]["model_type"] != "lgb_cb_rf":
            raise DomainError("ENSEMBLE_REQUIRED", "Веса доступны только для ансамбля CB/LGB/RF")
        names = {
            "CatBoostRegressor": "catboost",
            "LGBMRegressor": "lightgbm",
            "Booster": "lightgbm",
            "RandomForestRegressor": "random_forest",
        }
        values = weights.model_dump()
        model["models"] = [
            (values[names[type(estimator).__name__]], estimator) for _, estimator in model["models"]
        ]
        return self._save(model, manifest, {"parent_model_id": ident, "ensemble_weights": values})

    def _save(self, model, original, changes):
        ident = "model-" + digest({"original": original["id"], **changes})
        with self.store.lock("model-import"):
            if self.store.path("trained_models", ident).exists():
                return self.store.read("trained_models", ident)
            self.models.save(ident, model)
            manifest = {
                **original,
                **changes,
                "id": ident,
                "created_at": datetime.now(TZ).isoformat(),
                "artifact_sha256": file_hash(self.store.path("trained_models", ident, "joblib")),
                "quality_status": "requires_evaluation",
            }
            self.store.put("trained_models", ident, manifest)
            return manifest
