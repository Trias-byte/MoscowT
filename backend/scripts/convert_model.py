"""Explicit offline conversion for trusted local service artifacts, never an HTTP pickle loader."""

import argparse
import json
from pathlib import Path

import joblib

from moscowt.modeling.packages import ModelPackageService

parser = argparse.ArgumentParser(description="Convert a trusted local Potok model to a portable package")
parser.add_argument("source", type=Path)
parser.add_argument("--manifest", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument(
    "--trust-local-pickle",
    action="store_true",
    required=True,
    help="Explicitly allow Python deserialization of this trusted local file",
)
args = parser.parse_args()

model = joblib.load(args.source)
manifest = json.loads(args.manifest.read_text())
args.output.write_bytes(ModelPackageService.encode(manifest, model))
print(args.output)
