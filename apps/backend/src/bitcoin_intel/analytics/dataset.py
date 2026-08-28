from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import duckdb
import pyarrow.parquet as pq

from bitcoin_intel.analytics.views import register_analytical_views
from bitcoin_intel.ingestion.parquet import (
    PART_FILE_NAME,
    SCHEMA_VERSION,
    TABLE_DEFINITIONS,
)

CANONICAL_TABLES = tuple(TABLE_DEFINITIONS)
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})


class AnalyticalDatasetError(RuntimeError):
    """Raised when a canonical dataset cannot be opened safely for analytics."""


@dataclass(frozen=True, slots=True)
class ManifestTable:
    path: Path
    rows: int


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    schema_version: str
    tables: Mapping[str, ManifestTable]


class AnalyticalDataset:
    """A validated Phase 1 Parquet dataset exposed through scoped DuckDB sessions."""

    def __init__(self, path: Path) -> None:
        try:
            root = path.expanduser().resolve(strict=True)
        except OSError as error:
            raise AnalyticalDatasetError(f"analytical dataset does not exist: {path}") from error
        if not root.is_dir():
            raise AnalyticalDatasetError(f"analytical dataset is not a directory: {root}")
        self._path = root
        self._manifest = _load_manifest(root)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def manifest(self) -> DatasetManifest:
        return self._manifest

    @contextmanager
    def connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute("SET TimeZone = 'UTC'")
            for table_name in CANONICAL_TABLES:
                table_path = self._manifest.tables[table_name].path
                connection.read_parquet(str(table_path)).create_view(table_name)
            register_analytical_views(connection)
        except duckdb.Error as error:
            connection.close()
            raise AnalyticalDatasetError(f"failed to register analytical views: {error}") from error

        try:
            yield connection
        finally:
            connection.close()


def _load_manifest(root: Path) -> DatasetManifest:
    manifest_path = root / "manifest.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AnalyticalDatasetError(f"dataset manifest is missing: {manifest_path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnalyticalDatasetError(
            f"dataset manifest is unreadable or malformed: {error}"
        ) from error
    if not isinstance(raw_manifest, dict):
        raise AnalyticalDatasetError("dataset manifest must be a JSON object")

    schema_version = raw_manifest.get("schema_version")
    if not isinstance(schema_version, str) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise AnalyticalDatasetError(
            f"unsupported dataset schema version {schema_version!r}; supported: {supported}"
        )

    raw_tables = raw_manifest.get("output_tables")
    if not isinstance(raw_tables, dict) or set(raw_tables) != set(CANONICAL_TABLES):
        raise AnalyticalDatasetError(
            "manifest output_tables does not match the canonical table set"
        )

    tables: dict[str, ManifestTable] = {}
    for table_name in CANONICAL_TABLES:
        raw_table = raw_tables.get(table_name)
        if not isinstance(raw_table, dict):
            raise AnalyticalDatasetError(f"manifest entry for {table_name} must be an object")
        expected_relative_path = Path(table_name) / PART_FILE_NAME
        if raw_table.get("file") != expected_relative_path.as_posix():
            raise AnalyticalDatasetError(
                f"manifest entry for {table_name} has an unsupported Parquet layout"
            )
        rows = raw_table.get("rows")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise AnalyticalDatasetError(f"manifest row count for {table_name} is invalid")

        table_path = _resolve_dataset_file(root, expected_relative_path)
        expected_schema = TABLE_DEFINITIONS[table_name].arrow_schema
        try:
            actual_schema = pq.read_schema(table_path)
        except (OSError, ValueError) as error:
            raise AnalyticalDatasetError(
                f"canonical Parquet file for {table_name} is unreadable: {error}"
            ) from error
        if not actual_schema.equals(expected_schema, check_metadata=False):
            raise AnalyticalDatasetError(f"canonical Parquet schema mismatch for {table_name}")
        tables[table_name] = ManifestTable(path=table_path, rows=rows)

    return DatasetManifest(schema_version, MappingProxyType(tables))


def _resolve_dataset_file(root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute():
        raise AnalyticalDatasetError("manifest Parquet paths must be relative")
    try:
        resolved = (root / relative_path).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise AnalyticalDatasetError(
            f"required canonical Parquet file is missing or escapes the dataset: {relative_path}"
        ) from error
    if not resolved.is_file():
        raise AnalyticalDatasetError(f"canonical Parquet path is not a file: {relative_path}")
    return resolved
