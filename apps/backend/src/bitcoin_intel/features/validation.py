from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.features.definitions import definition_registry_sha256
from bitcoin_intel.features.models import (
    DEFINITIONS_FILE_NAME,
    FEATURE_CALCULATION_VERSION,
    FEATURE_SCHEMA_VERSION,
    FEATURE_TABLES,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    FeatureValidationIssue,
    FeatureValidationReport,
)
from bitcoin_intel.graph.constants import GRAPH_SCHEMA_VERSION


class FeatureStoreError(RuntimeError):
    """Raised when feature metadata cannot be interpreted safely."""


def validate_feature_store(feature_path: Path, dataset_path: Path) -> FeatureValidationReport:
    root = _resolve_directory(feature_path)
    dataset = AnalyticalDataset(dataset_path)
    manifest = _load_manifest(root)
    issues: list[FeatureValidationIssue] = []

    _mismatch(
        issues,
        "FEATURE_SCHEMA_VERSION_MISMATCH",
        manifest.get("feature_schema_version") != FEATURE_SCHEMA_VERSION,
        "feature schema version is unsupported",
    )
    _mismatch(
        issues,
        "FEATURE_CALCULATION_VERSION_MISMATCH",
        manifest.get("feature_calculation_version") != FEATURE_CALCULATION_VERSION,
        "feature calculation version is unsupported",
    )
    _mismatch(
        issues,
        "GRAPH_SCHEMA_VERSION_MISMATCH",
        manifest.get("graph_schema_version") != GRAPH_SCHEMA_VERSION,
        "graph projection schema version is unsupported",
    )
    _mismatch(
        issues,
        "CANONICAL_SCHEMA_VERSION_MISMATCH",
        manifest.get("canonical_schema_version") != dataset.manifest.schema_version,
        "canonical schema version differs from the supplied dataset",
    )
    _mismatch(
        issues,
        "CANONICAL_MANIFEST_HASH_MISMATCH",
        manifest.get("canonical_manifest_sha256") != _sha256_file(dataset.path / "manifest.json"),
        "feature store was not derived from the supplied canonical manifest",
    )
    semantic_identity = {
        key: manifest.get(key)
        for key in (
            "canonical_manifest_sha256",
            "canonical_schema_version",
            "graph_schema_version",
            "feature_schema_version",
            "feature_calculation_version",
            "feature_definitions_sha256",
            "build_configuration",
        )
    }
    expected_dataset_id = hashlib.sha256(
        json.dumps(semantic_identity, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    _mismatch(
        issues,
        "FEATURE_DATASET_ID_MISMATCH",
        manifest.get("feature_dataset_id") != expected_dataset_id,
        "feature dataset semantic identity is invalid",
    )
    definitions_path = root / DEFINITIONS_FILE_NAME
    definitions_hash = _sha256_file(definitions_path) if definitions_path.is_file() else None
    _mismatch(
        issues,
        "FEATURE_DEFINITIONS_MISSING_OR_CHANGED",
        definitions_hash != definition_registry_sha256()
        or manifest.get("feature_definitions_sha256") != definitions_hash,
        "feature definitions are missing or do not match the declared registry",
    )

    raw_tables = manifest.get("output_tables")
    if not isinstance(raw_tables, dict) or set(raw_tables) != set(FEATURE_TABLES):
        raise FeatureStoreError("manifest output_tables does not match the feature table contract")

    valid_paths: dict[str, Path] = {}
    values_are_queryable = True
    for table_name, definition in FEATURE_TABLES.items():
        raw_table = raw_tables.get(table_name)
        if not isinstance(raw_table, dict):
            raise FeatureStoreError(f"manifest table entry is malformed: {table_name}")
        expected_relative = Path(table_name) / PART_FILE_NAME
        if raw_table.get("file") != expected_relative.as_posix():
            raise FeatureStoreError(f"manifest table path is unsupported: {table_name}")
        table_path = _resolve_child(root, expected_relative)
        valid_paths[table_name] = table_path
        try:
            parquet = pq.ParquetFile(table_path)
            actual_schema = parquet.schema_arrow
            actual_rows = parquet.metadata.num_rows
        except (OSError, ValueError) as error:
            values_are_queryable = False
            issues.append(
                FeatureValidationIssue("UNREADABLE_FEATURE_PARQUET", 1, f"{table_name}: {error}")
            )
            continue
        schema_mismatch = not actual_schema.equals(definition.schema, check_metadata=False)
        _mismatch(
            issues,
            "FEATURE_SCHEMA_MISMATCH",
            schema_mismatch,
            f"{table_name} has an unexpected Parquet schema",
        )
        values_are_queryable = values_are_queryable and not schema_mismatch
        _mismatch(
            issues,
            "FEATURE_ROW_COUNT_MISMATCH",
            raw_table.get("rows") != actual_rows,
            f"{table_name} row count differs from the manifest",
            abs(_safe_int(raw_table.get("rows")) - actual_rows),
        )
        _mismatch(
            issues,
            "FEATURE_FILE_SIZE_MISMATCH",
            raw_table.get("bytes") != table_path.stat().st_size,
            f"{table_name} byte size differs from the manifest",
        )
        _mismatch(
            issues,
            "FEATURE_FILE_HASH_MISMATCH",
            raw_table.get("sha256") != _sha256_file(table_path),
            f"{table_name} SHA-256 differs from the manifest",
        )

    if len(valid_paths) == len(FEATURE_TABLES) and values_are_queryable:
        _validate_values(valid_paths, dataset, manifest, issues)
    return FeatureValidationReport(tuple(issues))


def _validate_values(
    paths: dict[str, Path],
    dataset: AnalyticalDataset,
    manifest: dict[str, Any],
    issues: list[FeatureValidationIssue],
) -> None:
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        for table_name, path in paths.items():
            connection.read_parquet(str(path)).create_view(table_name)
        for table_name, table in dataset.manifest.tables.items():
            connection.read_parquet(str(table.path)).create_view(table_name)

        checks = (
            (
                "DUPLICATE_TRANSACTION_FEATURE",
                "SELECT count(*)-count(DISTINCT txid) FROM transaction_features",
                "transaction feature TXIDs are not unique",
            ),
            (
                "DUPLICATE_ADDRESS_FEATURE",
                "SELECT count(*)-count(DISTINCT address) FROM address_features",
                "address feature keys are not unique",
            ),
            (
                "DUPLICATE_IP_FEATURE",
                "SELECT count(*)-count(DISTINCT ip) FROM ip_features",
                "IP feature keys are not unique",
            ),
            (
                "DUPLICATE_CORRELATION_FEATURE",
                "SELECT count(*)-count(DISTINCT address) FROM correlation_features",
                "correlation feature keys are not unique",
            ),
            (
                "UNKNOWN_TRANSACTION_FEATURE",
                """SELECT count(*) FROM transaction_features f
                LEFT JOIN transactions t USING (txid) WHERE t.txid IS NULL""",
                "transaction feature references an unknown canonical TXID",
            ),
            (
                "UNKNOWN_ADDRESS_FEATURE",
                """SELECT count(*) FROM address_features f LEFT JOIN (
                SELECT address FROM transaction_inputs
                UNION SELECT address FROM transaction_outputs
                ) a USING (address) WHERE a.address IS NULL""",
                "address feature references an unknown canonical address",
            ),
            (
                "UNKNOWN_IP_FEATURE",
                """SELECT count(*) FROM ip_features f LEFT JOIN (
                SELECT src_ip AS ip FROM network_observations
                UNION SELECT dst_ip AS ip FROM network_observations
                ) i USING (ip) WHERE i.ip IS NULL""",
                "IP feature references an unknown canonical IP",
            ),
            (
                "CORRELATION_ADDRESS_SET_MISMATCH",
                """SELECT count(*) FROM (
                (SELECT address FROM address_features
                 EXCEPT SELECT address FROM correlation_features)
                UNION ALL
                (SELECT address FROM correlation_features
                 EXCEPT SELECT address FROM address_features))""",
                "address and correlation feature identities differ",
            ),
        )
        for code, sql, message in checks:
            _query_issue(connection, issues, code, sql, message)

        for table_name, definition in FEATURE_TABLES.items():
            nonnegative = [
                field.name
                for field in definition.schema
                if field.name.endswith("_count")
                or field.name.endswith("_sats")
                or field.name.endswith("_seconds")
                or field.name.endswith("_size")
                or field.name.startswith("max_observations_")
            ]
            if nonnegative:
                predicate = " OR ".join(f'"{name}" < 0' for name in nonnegative)
                _query_issue(
                    connection,
                    issues,
                    "NEGATIVE_FEATURE_VALUE",
                    f'SELECT count(*) FROM "{table_name}" WHERE {predicate}',
                    f"{table_name} contains a negative constrained measurement",
                )
            floats = [field.name for field in definition.schema if str(field.type) == "double"]
            if floats:
                predicate = " OR ".join(
                    f'("{name}" IS NOT NULL AND NOT isfinite("{name}"))' for name in floats
                )
                _query_issue(
                    connection,
                    issues,
                    "NONFINITE_FEATURE_VALUE",
                    f'SELECT count(*) FROM "{table_name}" WHERE {predicate}',
                    f"{table_name} contains NaN or infinity",
                )

        cutoff = _manifest_cutoff(manifest)
        if cutoff is not None:
            for table_name in ("transaction_features", "address_features", "ip_features"):
                _query_issue(
                    connection,
                    issues,
                    "FEATURE_AFTER_CUTOFF",
                    f"SELECT count(*) FROM {table_name} WHERE last_observed_at > ?",
                    f"{table_name} includes observations after its cutoff",
                    [cutoff],
                )
    finally:
        connection.close()


def _query_issue(
    connection: duckdb.DuckDBPyConnection,
    issues: list[FeatureValidationIssue],
    code: str,
    sql: str,
    message: str,
    parameters: list[object] | None = None,
) -> None:
    row = connection.execute(sql, parameters or []).fetchone()
    if row is None:
        raise AssertionError(f"feature validation check returned no row: {code}")
    count = int(row[0])
    if count:
        issues.append(FeatureValidationIssue(code, count, message))


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_FILE_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FeatureStoreError(f"feature manifest is unreadable or malformed: {error}") from error
    if not isinstance(value, dict):
        raise FeatureStoreError("feature manifest must be a JSON object")
    return value


def _manifest_cutoff(manifest: dict[str, Any]) -> datetime | None:
    config = manifest.get("build_configuration")
    if not isinstance(config, dict):
        raise FeatureStoreError("feature manifest build_configuration is malformed")
    value = config.get("cutoff")
    if value is None:
        return None
    if not isinstance(value, str):
        raise FeatureStoreError("feature cutoff must be an ISO-8601 string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FeatureStoreError("feature cutoff is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeatureStoreError("feature cutoff must include a timezone")
    return parsed.astimezone(UTC)


def _resolve_directory(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise FeatureStoreError(f"feature store does not exist: {path}") from error
    if not root.is_dir():
        raise FeatureStoreError(f"feature store is not a directory: {root}")
    return root


def _resolve_child(root: Path, relative: Path) -> Path:
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise FeatureStoreError(
            f"feature file is missing or escapes its root: {relative}"
        ) from error
    if not path.is_file():
        raise FeatureStoreError(f"feature path is not a file: {relative}")
    return path


def _mismatch(
    issues: list[FeatureValidationIssue],
    code: str,
    condition: bool,
    message: str,
    count: int = 1,
) -> None:
    if condition:
        issues.append(FeatureValidationIssue(code, max(count, 1), message))


def _safe_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
