from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import joblib

from bitcoin_intel.ml.models import EXPERIMENT_SCHEMA_VERSION, MLExperimentError


def semantic_experiment_id(configuration: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(configuration)).hexdigest()


def create_staging_directory(output_root: Path, experiment_id: str) -> tuple[Path, Path]:
    root = output_root.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / experiment_id
    if destination.exists():
        raise MLExperimentError(
            f"experiment output already exists and will not be overwritten: {destination}"
        )
    staging = Path(tempfile.mkdtemp(prefix=f".{experiment_id}.tmp-", dir=root))
    return staging, destination


def publish_staging_directory(staging: Path, destination: Path) -> None:
    if destination.exists():
        raise MLExperimentError(
            f"experiment output was created concurrently and will not be overwritten: {destination}"
        )
    staging.replace(destination)


def discard_staging_directory(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact_metadata(root: Path, relative_paths: list[Path]) -> dict[str, dict[str, Any]]:
    return {
        path.as_posix(): {
            "bytes": (root / path).stat().st_size,
            "sha256": sha256_file(root / path),
        }
        for path in relative_paths
    }


def inspect_experiment(experiment_path: Path) -> dict[str, Any]:
    root, manifest = _read_manifest(experiment_path)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise MLExperimentError("experiment manifest artifacts are malformed")
    for relative, expected in artifacts.items():
        if not isinstance(relative, str) or not isinstance(expected, dict):
            raise MLExperimentError("experiment artifact entry is malformed")
        path = _resolve_child(root, Path(relative))
        if expected.get("sha256") != sha256_file(path):
            raise MLExperimentError(f"experiment artifact hash mismatch: {relative}")
        if expected.get("bytes") != path.stat().st_size:
            raise MLExperimentError(f"experiment artifact size mismatch: {relative}")
    metrics_path = _resolve_child(root, Path("metrics.json"))
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MLExperimentError(f"experiment metrics are unreadable: {error}") from error
    return {"experiment": manifest, "metrics": metrics, "valid": True}


def load_trusted_local_model(experiment_path: Path, *, trusted: bool = False) -> Any:
    """Load only a hash-verified local artifact after an explicit trust decision.

    Joblib uses pickle-compatible deserialization and can execute code. Callers must never set
    ``trusted=True`` for downloaded, uploaded, or otherwise untrusted experiment directories.
    """

    if not trusted:
        raise MLExperimentError(
            "joblib deserialization refused: locally generated artifact was not explicitly trusted"
        )
    root, manifest = _read_manifest(experiment_path)
    artifacts = manifest.get("artifacts")
    model_meta = artifacts.get("model.joblib") if isinstance(artifacts, dict) else None
    if not isinstance(model_meta, dict):
        raise MLExperimentError("experiment manifest does not declare model.joblib")
    model_path = _resolve_child(root, Path("model.joblib"))
    if model_meta.get("sha256") != sha256_file(model_path):
        raise MLExperimentError("model artifact hash mismatch; refusing deserialization")
    try:
        return joblib.load(model_path)
    except Exception as error:
        raise MLExperimentError(f"trusted local model could not be loaded: {error}") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(experiment_path: Path) -> tuple[Path, dict[str, Any]]:
    try:
        root = experiment_path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise MLExperimentError(f"experiment path is not a directory: {root}")
        manifest = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MLExperimentError(
            f"experiment manifest is unreadable or malformed: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise MLExperimentError("experiment manifest must be a JSON object")
    if manifest.get("experiment_schema_version") != EXPERIMENT_SCHEMA_VERSION:
        raise MLExperimentError("experiment schema version is unsupported")
    semantic = manifest.get("semantic_configuration")
    if not isinstance(semantic, dict):
        raise MLExperimentError("experiment semantic configuration is malformed")
    if manifest.get("experiment_id") != semantic_experiment_id(semantic):
        raise MLExperimentError("experiment semantic identity is invalid")
    return root, manifest


def _resolve_child(root: Path, relative: Path) -> Path:
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise MLExperimentError(
            f"experiment artifact is missing or escapes root: {relative}"
        ) from error
    if not path.is_file():
        raise MLExperimentError(f"experiment artifact is not a file: {relative}")
    return path


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
