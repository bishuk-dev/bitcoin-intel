from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bitcoin_intel.benchmarking.scenarios import SCENARIO_NAMES
from bitcoin_intel.features.models import (
    FEATURE_SCHEMA_VERSION_V1,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    SUPPORTED_FEATURE_SCHEMA_VERSIONS,
    feature_tables_for_version,
)
from bitcoin_intel.ml.models import (
    ENRICHMENT_FEATURES,
    ENTITY_ID_COLUMN,
    FEATURE_FAMILIES,
    TIME_COLUMN,
    MLExperimentError,
)

_FORBIDDEN_MODEL_COLUMNS = frozenset(
    {
        "txid",
        "address",
        "ip",
        "scenario_class",
        "scenario_group_id",
        "source_record_id",
        "feature_dataset_id",
        "first_observed_at",
        "last_observed_at",
    }
)


@dataclass(frozen=True, slots=True)
class LoadedExperimentDataset:
    entity_ids: np.ndarray[Any, np.dtype[np.str_]]
    values: np.ndarray[Any, np.dtype[np.float64]]
    times: np.ndarray[Any, np.dtype[np.datetime64]]
    feature_columns: tuple[str, ...]
    labels: np.ndarray[Any, np.dtype[np.str_]] | None
    groups: np.ndarray[Any, np.dtype[np.str_]] | None
    intensities: np.ndarray[Any, np.dtype[np.str_]] | None
    secondary_tags: tuple[tuple[str, ...], ...] | None
    feature_manifest: dict[str, Any]
    truth_metadata: dict[str, Any] | None


def load_experiment_dataset(
    feature_path: Path,
    feature_family: str,
    truth_path: Path | None,
) -> LoadedExperimentDataset:
    root, manifest = _load_feature_manifest(feature_path)
    columns = FEATURE_FAMILIES[feature_family]
    if manifest["feature_schema_version"] == FEATURE_SCHEMA_VERSION_V1:
        if feature_family in {"transaction-network-enrichment", "cross-layer"}:
            raise MLExperimentError(
                f"feature family {feature_family!r} requires Feature Schema v2 enrichment columns"
            )
        columns = tuple(name for name in columns if name not in ENRICHMENT_FEATURES)
    audit_feature_columns(columns)
    table_path = root / "transaction_features" / PART_FILE_NAME
    _verify_feature_table(table_path, manifest)
    selected = [ENTITY_ID_COLUMN, TIME_COLUMN, *columns]
    try:
        table = pq.read_table(table_path, columns=selected)
    except (OSError, ValueError, pa.ArrowException) as error:
        raise MLExperimentError(f"transaction feature table is unreadable: {error}") from error
    if table.num_rows < 3:
        raise MLExperimentError("an experiment requires at least three transaction feature rows")

    entity_ids = np.asarray(table[ENTITY_ID_COLUMN].to_pylist(), dtype=np.str_)
    if len(set(entity_ids.tolist())) != len(entity_ids):
        raise MLExperimentError("transaction feature identities are not unique")
    times = _timestamps(table[TIME_COLUMN])
    values = np.column_stack([_numeric_column(table[name], name) for name in columns])
    if np.isinf(values).any():
        raise MLExperimentError("model feature matrix contains infinity")

    labels: np.ndarray[Any, np.dtype[np.str_]] | None = None
    groups: np.ndarray[Any, np.dtype[np.str_]] | None = None
    truth_metadata: dict[str, Any] | None = None
    intensities: np.ndarray[Any, np.dtype[np.str_]] | None = None
    secondary_tags: tuple[tuple[str, ...], ...] | None = None
    if truth_path is not None:
        labels, groups, intensities, secondary_tags, truth_metadata = _load_and_join_truth(
            truth_path, entity_ids
        )
    return LoadedExperimentDataset(
        entity_ids=entity_ids,
        values=values,
        times=times,
        feature_columns=columns,
        labels=labels,
        groups=groups,
        intensities=intensities,
        secondary_tags=secondary_tags,
        feature_manifest=manifest,
        truth_metadata=truth_metadata,
    )


def audit_feature_columns(columns: tuple[str, ...]) -> None:
    duplicates = sorted({name for name in columns if columns.count(name) > 1})
    if duplicates:
        raise MLExperimentError(f"duplicate model feature columns: {', '.join(duplicates)}")
    forbidden = sorted(set(columns) & _FORBIDDEN_MODEL_COLUMNS)
    if forbidden:
        raise MLExperimentError(f"leakage audit rejected columns: {', '.join(forbidden)}")
    eligible = set(FEATURE_FAMILIES["all-eligible"])
    unknown = sorted(set(columns) - eligible)
    if unknown:
        raise MLExperimentError(f"feature policy contains unknown columns: {', '.join(unknown)}")


def _load_feature_manifest(feature_path: Path) -> tuple[Path, dict[str, Any]]:
    try:
        root = feature_path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise MLExperimentError(f"feature path is not a directory: {root}")
        manifest_path = root / MANIFEST_FILE_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MLExperimentError(f"feature manifest is unreadable or malformed: {error}") from error
    if not isinstance(manifest, dict):
        raise MLExperimentError("feature manifest must be a JSON object")
    if manifest.get("feature_schema_version") not in SUPPORTED_FEATURE_SCHEMA_VERSIONS:
        raise MLExperimentError("feature schema version is unsupported")
    build = manifest.get("build_configuration")
    if not isinstance(build, dict) or build.get("temporal_mode") not in {"snapshot", "cutoff"}:
        raise MLExperimentError("feature manifest has invalid temporal-mode metadata")
    if build["temporal_mode"] == "cutoff" and not isinstance(build.get("cutoff"), str):
        raise MLExperimentError("cutoff feature manifest is missing its cutoff timestamp")
    return root, manifest


def _verify_feature_table(path: Path, manifest: dict[str, Any]) -> None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(path.parents[1].resolve(strict=True))
        raw_table = manifest["output_tables"]["transaction_features"]
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise MLExperimentError("transaction feature metadata is missing or malformed") from error
    if not isinstance(raw_table, dict):
        raise MLExperimentError("transaction feature metadata is missing or malformed")
    if raw_table.get("file") != "transaction_features/part-00000.parquet":
        raise MLExperimentError("transaction feature path does not match the schema contract")
    if raw_table.get("sha256") != _sha256_file(resolved):
        raise MLExperimentError("transaction feature file hash differs from its manifest")
    schema = pq.read_schema(resolved)
    version = manifest.get("feature_schema_version")
    if not isinstance(version, str):
        raise MLExperimentError("feature schema version is unsupported")
    expected = feature_tables_for_version(version)["transaction_features"].schema
    if not schema.equals(expected, check_metadata=False):
        raise MLExperimentError("transaction feature table has an unsupported schema")


def _numeric_column(column: pa.ChunkedArray, name: str) -> np.ndarray[Any, np.dtype[np.float64]]:
    if not (pa.types.is_integer(column.type) or pa.types.is_floating(column.type)):
        raise MLExperimentError(f"model feature is not numeric: {name}")
    cast = pc.cast(column, pa.float64())
    return np.asarray(cast.to_numpy(zero_copy_only=False), dtype=np.float64)


def _timestamps(column: pa.ChunkedArray) -> np.ndarray[Any, np.dtype[np.datetime64]]:
    if column.null_count:
        raise MLExperimentError("temporal splitting metadata contains null timestamps")
    values = np.asarray(column.to_numpy(zero_copy_only=False)).astype("datetime64[us]")
    return values


def _load_and_join_truth(
    truth_path: Path,
    entity_ids: np.ndarray[Any, np.dtype[np.str_]],
) -> tuple[
    np.ndarray[Any, np.dtype[np.str_]],
    np.ndarray[Any, np.dtype[np.str_]],
    np.ndarray[Any, np.dtype[np.str_]] | None,
    tuple[tuple[str, ...], ...] | None,
    dict[str, Any],
]:
    try:
        path = truth_path.expanduser().resolve(strict=True)
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MLExperimentError(f"scenario truth is unreadable or malformed: {error}") from error
    if not isinstance(document, dict) or document.get("truth_schema_version") not in {
        "1.1.0",
        "1.2.0",
    }:
        raise MLExperimentError("scenario truth schema version is unsupported")
    if document.get("not_criminal_ground_truth") is not True:
        raise MLExperimentError("scenario truth must declare its non-criminal semantics")
    raw_rows = document.get("transactions")
    if not isinstance(raw_rows, list):
        raise MLExperimentError("scenario truth transactions must be a list")
    by_id: dict[str, tuple[str, str, str, tuple[str, ...]]] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise MLExperimentError("scenario truth contains a malformed transaction")
        txid = raw.get("txid")
        label = raw.get("scenario_class")
        group = raw.get("scenario_group_id")
        if not isinstance(txid, str) or not isinstance(group, str) or not group:
            raise MLExperimentError("scenario truth identity/group metadata is malformed")
        if label not in SCENARIO_NAMES:
            raise MLExperimentError(f"scenario truth contains an unsupported class: {label!r}")
        if txid in by_id:
            raise MLExperimentError(f"scenario truth contains duplicate TXID: {txid}")
        intensity = raw.get("scenario_intensity", "not_available")
        secondary = raw.get("secondary_tags", [])
        if (
            not isinstance(intensity, str)
            or not isinstance(secondary, list)
            or not all(
                isinstance(value, str) and value in SCENARIO_NAMES and value != "baseline"
                for value in secondary
            )
        ):
            raise MLExperimentError("scenario truth challenge metadata is malformed")
        by_id[txid] = (str(label), group, intensity, tuple(secondary))
    missing = [txid for txid in entity_ids.tolist() if txid not in by_id]
    if missing:
        raise MLExperimentError(
            f"scenario truth is missing {len(missing)} transaction feature identities"
        )
    labels = np.asarray([by_id[txid][0] for txid in entity_ids.tolist()], dtype=np.str_)
    groups = np.asarray([by_id[txid][1] for txid in entity_ids.tolist()], dtype=np.str_)
    is_challenge = document["truth_schema_version"] == "1.2.0"
    intensities = (
        np.asarray([by_id[txid][2] for txid in entity_ids.tolist()], dtype=np.str_)
        if is_challenge
        else None
    )
    secondary_tags = tuple(by_id[txid][3] for txid in entity_ids.tolist()) if is_challenge else None
    metadata = {
        "path_sha256": _sha256_file(path),
        "truth_schema_version": document["truth_schema_version"],
        "purpose": document.get("purpose"),
        "not_criminal_ground_truth": True,
        "configuration": document.get("configuration"),
        "challenge_profile": (
            document.get("configuration", {}).get("profile")
            if isinstance(document.get("configuration"), dict)
            else None
        ),
    }
    return labels, groups, intensities, secondary_tags, metadata


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
