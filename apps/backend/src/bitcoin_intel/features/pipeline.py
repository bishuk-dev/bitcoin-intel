from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.analytics.validation import validate_analytical_dataset
from bitcoin_intel.enrichment.models import MANIFEST_FILE_NAME as ENRICHMENT_MANIFEST_FILE_NAME
from bitcoin_intel.enrichment.models import PART_FILE_NAME as ENRICHMENT_PART_FILE_NAME
from bitcoin_intel.enrichment.models import TABLE_NAME as ENRICHMENT_TABLE_NAME
from bitcoin_intel.enrichment.validation import validate_enrichment_store
from bitcoin_intel.features.definitions import (
    definition_registry_sha256,
    serialize_definition_registry,
)
from bitcoin_intel.features.graph import build_component_size_table
from bitcoin_intel.features.models import (
    DEFINITIONS_FILE_NAME,
    FEATURE_CALCULATION_VERSION,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION_V1,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    FeatureBuildConfig,
    FeatureBuildSummary,
    feature_tables_for_version,
)
from bitcoin_intel.features.queries import FEATURE_QUERIES_V1, FEATURE_QUERIES_V2
from bitcoin_intel.graph.constants import GRAPH_SCHEMA_VERSION

_LOGGER = logging.getLogger(__name__)
_ROW_GROUP_SIZE = 65_536


class FeatureBuildError(RuntimeError):
    """Raised when a feature store cannot be built or published safely."""


def build_features(
    dataset_path: Path,
    output_path: Path,
    enrichment_path: Path,
    config: FeatureBuildConfig | None = None,
) -> FeatureBuildSummary:
    return _build_features(
        dataset_path, output_path, FEATURE_SCHEMA_VERSION, enrichment_path, config
    )


def build_features_v1(
    dataset_path: Path,
    output_path: Path,
    config: FeatureBuildConfig | None = None,
) -> FeatureBuildSummary:
    """Build the historical v1 contract for compatibility and reproducibility."""
    return _build_features(dataset_path, output_path, FEATURE_SCHEMA_VERSION_V1, None, config)


def _build_features(
    dataset_path: Path,
    output_path: Path,
    schema_version: str,
    enrichment_path: Path | None,
    config: FeatureBuildConfig | None,
) -> FeatureBuildSummary:
    effective_config = config or FeatureBuildConfig()
    dataset = AnalyticalDataset(dataset_path)
    integrity = validate_analytical_dataset(dataset)
    if not integrity.is_valid:
        codes = ", ".join(issue.code for issue in integrity.issues)
        raise FeatureBuildError(f"canonical dataset failed integrity validation: {codes}")
    enrichment_root: Path | None = None
    enrichment_manifest: dict[str, Any] | None = None
    if schema_version == FEATURE_SCHEMA_VERSION:
        if enrichment_path is None:
            raise FeatureBuildError("Feature Schema v2 requires an enrichment store")
        enrichment_report = validate_enrichment_store(enrichment_path, dataset.path)
        if not enrichment_report.is_valid:
            codes = ", ".join(issue.code for issue in enrichment_report.issues)
            raise FeatureBuildError(f"enrichment store failed validation: {codes}")
        enrichment_root = enrichment_path.expanduser().resolve(strict=True)
        enrichment_manifest = json.loads(
            (enrichment_root / ENRICHMENT_MANIFEST_FILE_NAME).read_text(encoding="utf-8")
        )

    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FeatureBuildError(
            f"feature output already exists and will not be overwritten: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        canonical_manifest_path = dataset.path / "manifest.json"
        canonical_manifest_sha256 = _sha256_file(canonical_manifest_path)
        definitions_bytes = serialize_definition_registry(schema_version)
        (staging / DEFINITIONS_FILE_NAME).write_bytes(definitions_bytes)

        table_metadata: dict[str, dict[str, Any]] = {}
        with dataset.connect() as connection:
            if enrichment_root is not None:
                connection.read_parquet(
                    str(enrichment_root / ENRICHMENT_TABLE_NAME / ENRICHMENT_PART_FILE_NAME)
                ).create_view(ENRICHMENT_TABLE_NAME)
            _register_scoped_views(connection, effective_config)
            component_sizes = build_component_size_table(connection)
            connection.register("graph_component_sizes", component_sizes)
            feature_tables = feature_tables_for_version(schema_version)
            feature_queries = (
                FEATURE_QUERIES_V2
                if schema_version == FEATURE_SCHEMA_VERSION
                else FEATURE_QUERIES_V1
            )
            for table_name, definition in feature_tables.items():
                table_path = staging / table_name / PART_FILE_NAME
                table_path.parent.mkdir(parents=True, exist_ok=False)
                parameters: list[object] | None = (
                    [effective_config.reused_ip_min_transactions]
                    if table_name == "correlation_features"
                    else None
                )
                rows = _write_query(
                    connection,
                    feature_queries[table_name],
                    parameters,
                    table_path,
                    definition.schema,
                )
                table_metadata[table_name] = {
                    "file": f"{table_name}/{PART_FILE_NAME}",
                    "rows": rows,
                    "bytes": table_path.stat().st_size,
                    "sha256": _sha256_file(table_path),
                }

        cutoff = _utc(effective_config.cutoff)
        build_configuration = {
            "temporal_mode": "snapshot" if cutoff is None else "cutoff",
            "cutoff": _timestamp(cutoff),
            "cutoff_basis": "network observation time",
            "transaction_admission": (
                "all canonical transactions"
                if cutoff is None
                else "transactions with at least one network observation at or before cutoff"
            ),
            "reused_ip_min_transactions": effective_config.reused_ip_min_transactions,
            "graph_projection": {
                "nodes": ["Address", "Transaction"],
                "relationships": ["SPENT_IN", "CREATED_OUTPUT"],
                "direction": "undirected for WCC",
                "weighting": "unweighted",
                "engine": "igraph",
                "persistence": "ephemeral",
            },
        }
        semantic_identity: dict[str, Any] = {
            "canonical_manifest_sha256": canonical_manifest_sha256,
            "canonical_schema_version": dataset.manifest.schema_version,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "feature_schema_version": schema_version,
            "feature_calculation_version": FEATURE_CALCULATION_VERSION,
            "feature_definitions_sha256": definition_registry_sha256(schema_version),
            "build_configuration": build_configuration,
        }
        if enrichment_manifest is not None:
            semantic_identity.update(
                {
                    "enrichment_dataset_id": enrichment_manifest["enrichment_dataset_id"],
                    "enrichment_schema_version": enrichment_manifest["enrichment_schema_version"],
                    "enrichment_manifest_sha256": _sha256_file(
                        enrichment_root / ENRICHMENT_MANIFEST_FILE_NAME
                    )
                    if enrichment_root is not None
                    else None,
                }
            )
        feature_dataset_id = _sha256_bytes(_canonical_json(semantic_identity))
        manifest = {
            "feature_dataset_id": feature_dataset_id,
            **semantic_identity,
            "built_at": _timestamp(datetime.now(UTC)),
            "output_tables": table_metadata,
        }
        (staging / MANIFEST_FILE_NAME).write_bytes(_pretty_json(manifest))

        from bitcoin_intel.features.validation import validate_feature_store

        build_report = validate_feature_store(staging, dataset.path, enrichment_root)
        if not build_report.is_valid:
            details = "; ".join(f"{issue.code}={issue.count}" for issue in build_report.issues)
            raise FeatureBuildError(f"staged feature store failed validation: {details}")
        if destination.exists():
            raise FeatureBuildError(
                "feature output was created concurrently and will not be overwritten: "
                f"{destination}"
            )
        staging.replace(destination)
        return FeatureBuildSummary(
            output_path=destination,
            feature_dataset_id=feature_dataset_id,
            temporal_mode=str(build_configuration["temporal_mode"]),
            cutoff=cutoff,
            table_rows={name: int(metadata["rows"]) for name, metadata in table_metadata.items()},
        )
    except duckdb.Error as error:
        _LOGGER.exception("feature query failed: dataset=%s", dataset.path)
        if staging.exists():
            shutil.rmtree(staging)
        raise FeatureBuildError(f"feature query failed: {error}") from error
    except Exception:
        _LOGGER.exception("feature build failed: dataset=%s", dataset.path)
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _register_scoped_views(
    connection: duckdb.DuckDBPyConnection, config: FeatureBuildConfig
) -> None:
    cutoff = _utc(config.cutoff)
    if cutoff is None:
        connection.execute("CREATE TEMP TABLE scoped_txids AS SELECT txid FROM transactions")
        connection.execute(
            "CREATE TEMP VIEW scoped_observations AS SELECT * FROM network_observations"
        )
    else:
        connection.execute(
            """CREATE TEMP TABLE scoped_txids AS
            SELECT DISTINCT txid FROM network_observations WHERE observed_at <= ?""",
            [cutoff],
        )
        connection.execute(
            """CREATE TEMP TABLE scoped_observations AS
            SELECT * FROM network_observations WHERE observed_at <= ?""",
            [cutoff],
        )
    connection.execute(
        """CREATE TEMP VIEW scoped_transactions AS
        SELECT t.* FROM transactions t JOIN scoped_txids USING (txid)"""
    )
    connection.execute(
        """CREATE TEMP VIEW scoped_inputs AS
        SELECT i.* FROM transaction_inputs i JOIN scoped_txids USING (txid)"""
    )
    connection.execute(
        """CREATE TEMP VIEW scoped_outputs AS
        SELECT o.* FROM transaction_outputs o JOIN scoped_txids USING (txid)"""
    )
    connection.execute(
        """CREATE TEMP VIEW address_transactions AS
        SELECT address, txid FROM scoped_inputs
        UNION
        SELECT address, txid FROM scoped_outputs"""
    )
    connection.execute(
        """CREATE TEMP VIEW address_observations AS
        SELECT a.address, o.observation_id, o.txid, o.observed_at
        FROM address_transactions a JOIN scoped_observations o USING (txid)"""
    )
    connection.execute(
        """CREATE TEMP VIEW ip_observations AS
        WITH endpoints AS (
            SELECT src_ip AS ip, observation_id, txid, observed_at,
                   true AS is_source, false AS is_destination,
                   src_port, NULL::BIGINT AS dst_port,
                   reported_asn, reported_geo_country
            FROM scoped_observations
            UNION ALL
            SELECT dst_ip AS ip, observation_id, txid, observed_at,
                   false AS is_source, true AS is_destination,
                   NULL::BIGINT AS src_port, dst_port,
                   reported_asn, reported_geo_country
            FROM scoped_observations
        )
        SELECT ip, observation_id, txid, observed_at,
               bool_or(is_source) AS is_source, bool_or(is_destination) AS is_destination,
               max(src_port) AS src_port, max(dst_port) AS dst_port,
               max(reported_asn) AS reported_asn,
               max(reported_geo_country) AS reported_geo_country
        FROM endpoints
        GROUP BY ip, observation_id, txid, observed_at"""
    )
    if ENRICHMENT_TABLE_NAME in {
        str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()
    }:
        connection.execute(
            """CREATE TEMP VIEW enriched_observations AS
            SELECT o.*,
                   src.enriched_country_code AS src_enriched_country_code,
                   src.enriched_asn AS src_enriched_asn,
                   dst.enriched_country_code AS dst_enriched_country_code,
                   dst.enriched_asn AS dst_enriched_asn
            FROM scoped_observations o
            JOIN ip_enrichment src ON src.ip = o.src_ip
            JOIN ip_enrichment dst ON dst.ip = o.dst_ip"""
        )


def _write_query(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    parameters: list[object] | None,
    output_path: Path,
    schema: pa.Schema,
) -> int:
    result = connection.execute(query, parameters or [])
    reader = result.to_arrow_reader(_ROW_GROUP_SIZE)
    rows = 0
    with pq.ParquetWriter(
        output_path,
        schema,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
        version="2.6",
    ) as writer:
        for batch in reader:
            table = pa.Table.from_batches([batch]).cast(schema, safe=True)
            writer.write_table(table, row_group_size=_ROW_GROUP_SIZE)
            rows += table.num_rows
    return rows


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
