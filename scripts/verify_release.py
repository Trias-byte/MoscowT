"""Verify bundled inputs using only the Python standard library."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path


class ReleaseVerifier:
    def __init__(self, directory: Path):
        self.directory = directory

    def manifest(self, name: str) -> dict:
        return json.loads((self.directory / name).read_text())

    @staticmethod
    def checksum(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def verify_file(self, name: str, checksum: str, size: int | None = None):
        path = self.directory / name
        if not path.is_file() or (size is not None and path.stat().st_size != size):
            raise ValueError(f"Missing or incomplete release file: {name}")
        if self.checksum(path) != checksum:
            raise ValueError(f"Release checksum mismatch: {name}")

    def verify(self):
        bundle = self.manifest("platform-demo.manifest.json")
        combined = hashlib.sha256()
        for part in bundle["parts"]:
            self.verify_file(part["path"], part["sha256"], part["bytes"])
            with (self.directory / part["path"]).open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    combined.update(chunk)
        if combined.hexdigest() != bundle["sha256"]:
            raise ValueError("Combined demo archive checksum mismatch")
        passengers = self.manifest("passengers.manifest.json")
        self.verify_file("passengers.tar.gz", passengers["sha256"], passengers["bytes"])
        baseline = self.manifest("competition-baseline.manifest.json")
        self.verify_file("competition-baseline.zip", baseline["package_sha256"])
        self.verify_file(
            "competition-template.csv",
            baseline["input_hashes"]["dataset/test_submission.csv"],
        )
        return bundle

    def assemble(self, output: Path, bundle: dict):
        output.parent.mkdir(parents=True, exist_ok=True)
        pending = output.with_suffix(output.suffix + ".tmp")
        try:
            with pending.open("wb") as target:
                for part in bundle["parts"]:
                    with (self.directory / part["path"]).open("rb") as source:
                        shutil.copyfileobj(source, target)
            pending.replace(output)
        finally:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deliverables",
    )
    parser.add_argument("--assemble", type=Path)
    args = parser.parse_args()
    verifier = ReleaseVerifier(args.directory)
    bundle = verifier.verify()
    if args.assemble:
        verifier.assemble(args.assemble, bundle)
    print("Release checksums verified")


if __name__ == "__main__":
    main()
