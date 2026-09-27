"""Build the app's native, data-only package from the frozen ML result. Run at repo root."""

import json
import sys
from pathlib import Path

import pandas as pd
from catboost import CatBoostRegressor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ML.data import labels  # noqa: E402

from moscowt.modeling.baseline_features import FEATURES, history_fingerprint  # noqa: E402
from moscowt.modeling.competition import SEEDS, CompetitionAdapter  # noqa: E402
from moscowt.modeling.contracts import TrainingSpec  # noqa: E402
from moscowt.modeling.packages import ModelPackageService  # noqa: E402
from moscowt.storage import atomic_write, file_hash, write_json  # noqa: E402

SUBMISSION_SHA256 = "4adcc1b06ccd53eeddee5637560919396483809fc2da696cae3c1cba013a8d95"


def main():
    run = ROOT / "ML/runs/20260927T185151Z"
    bundle = json.loads((run / "final/bundle.json").read_bytes())
    assert file_hash(ROOT / "ML/submission.csv") == SUBMISSION_SHA256
    panel = labels()
    history = pd.DataFrame(
        {
            "route": panel.route.astype(str),
            "timestamp": (panel.date + pd.to_timedelta(panel.hour, unit="h")).dt.tz_localize("Europe/Moscow"),
            "value": panel.boardings,
        }
    )
    routes = [str(r) for r in panel.route.unique()]
    spec = TrainingSpec(
        dataset_id="dataset-competition-2025",
        model_type="competition_catboost",
        route_ids=routes,
        time_range={"start": "2025-01-01T00:00:00+03:00", "end": "2025-11-01T00:00:00+03:00"},
    )
    models, hashes = [], {}
    for seed, entry in zip(SEEDS, bundle["entries"], strict=True):
        folder = run / entry["folder"]
        metadata = json.loads((folder / "model.json").read_bytes())
        assert metadata["features"] == FEATURES and metadata["seed"] == seed and entry["weight"] == 0.2
        assert metadata["config"] == {
            "name": "cb_base",
            "algorithm": "catboost",
            "scheme": "sparse14",
            "climate": False,
            "residual": False,
            "deduplicate": False,
            "iterations": 450,
            "depth": 6,
            "learning_rate": 0.04,
            "l2_leaf_reg": 8,
        }
        models.append((entry["weight"], CatBoostRegressor().load_model(str(folder / "model.cbm"))))
        hashes[str(seed)] = file_hash(folder / "model.cbm")
    zeros = sorted(history.groupby("route").value.sum().loc[lambda x: x == 0].index.tolist())
    model = {
        "models": models,
        "features": FEATURES,
        "routes": routes,
        "seeds": SEEDS,
        "start": spec.time_range.start.isoformat(),
        "zero_routes": zeros,
        "training_rows": metadata["training_rows"],
        "history_sha256": history_fingerprint(history),
    }
    evidence = {
        "score": 0.88,
        "metric": "max(0, 1 - WAPE)",
        "source": "user_reported_competition_score",
        "submission_sha256": SUBMISSION_SHA256,
        "origin": spec.time_range.end.isoformat(),
        "time_range": {"start": spec.time_range.end.isoformat(), "end": "2026-01-01T00:00:00+03:00"},
        "applies_to": "Exact submitted CSV only; retrained models have no competition score",
    }
    manifest = {
        "id": "model-competition-cb-5seed-v1",
        "spec": spec.model_dump(mode="json"),
        "label": "Базовая · CatBoost × 5",
        "recipe": CompetitionAdapter.version,
        "capabilities": CompetitionAdapter().capabilities(),
        "environment": bundle["environment"],
        "competition_result": evidence,
        "source_run": "ML/runs/20260927T185151Z",
        "source_models_sha256": hashes,
        "input_hashes": bundle["input_hashes"],
        "source_code_hashes": bundle["code_hashes"],
        "availability_policy": "retrospective_event_time",
        "training_rows": len(history),
        "supervised_rows": metadata["training_rows"],
    }
    output = ROOT / "deliverables/competition-baseline.zip"
    atomic_write(output, ModelPackageService.encode(manifest, model))
    write_json(output.with_suffix(".manifest.json"), {**manifest, "package_sha256": file_hash(output)})
    write_json(ROOT / "ML/competition_result.json", evidence)
    print(json.dumps({"path": str(output), "sha256": file_hash(output), "bytes": output.stat().st_size}))


if __name__ == "__main__":
    main()
