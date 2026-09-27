"""Run the untouched source only in an isolated process; retain fitted artifacts."""
import hashlib
import importlib.metadata
import json
import resource
import runpy
import time
from pathlib import Path

import joblib

started = time.monotonic()
namespace = runpy.run_path("/app/original.py", run_name="__main__")
models = {key: value for key, value in namespace.items() if key.startswith("model_") and hasattr(value, "predict")}
joblib.dump(models, "legacy_models.joblib", compress=3)
Path("reproduction.json").write_text(json.dumps({
    "script_sha256": hashlib.sha256(Path("/app/original.py").read_bytes()).hexdigest(),
    "features_sha256": hashlib.sha256(Path("dataset/features_v4.parquet").read_bytes()).hexdigest(),
    "output_sha256": hashlib.sha256(Path("dataset/submission.csv").read_bytes()).hexdigest(),
    "seconds": time.monotonic() - started,
    "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    "environment": {p: importlib.metadata.version(p) for p in ("numpy", "pandas", "catboost", "lightgbm", "scikit-learn", "holidays")},
    "models": list(models),
}, indent=2))

import pandas as pd
actual = pd.read_csv("dataset/submission.csv", sep=";")
reference = pd.read_csv("/reference.csv", sep=";")
keys = ["route", "date", "hour"]
comparison = actual.merge(reference, on=keys, suffixes=("_actual", "_reference"), validate="one_to_one")
report = json.loads(Path("reproduction.json").read_text())
report.update(keys_equal=len(comparison) == len(actual) == len(reference),
    predictions_equal=bool(comparison.prediction_actual.equals(comparison.prediction_reference)),
    differing_predictions=int((comparison.prediction_actual != comparison.prediction_reference).sum()),
    sum_actual=int(actual.prediction.sum()), sum_reference=int(reference.prediction.sum()),
    max_absolute_difference=int((comparison.prediction_actual - comparison.prediction_reference).abs().max()))
Path("reproduction.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
