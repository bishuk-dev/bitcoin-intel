from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.graph.constants import (
    GRAPH_SCHEMA_VERSION,
    NEO4J_VERSION,
    NODE_COUNTS,
    RELATIONSHIP_COUNTS,
    SUPPORTED_GRAPH_SCHEMA_VERSIONS,
)
from bitcoin_intel.graph.models import (
    GraphImportFile,
    GraphImportIssue,
    GraphImportManifest,
    GraphImportValidationReport,
    PreparedGraphImport,
)

_ROW_GROUP_SIZE = 65_536
_LOGGER = logging.getLogger(__name__)


class GraphImportError(RuntimeError):
    """Raised when a graph-import dataset cannot be built or validated safely."""


@dataclass(frozen=True, slots=True)
class _ImportDefinition:
    file_name: str
    count_name: str
    kind: str
    schema: pa.Schema
    query: str
    header: str

    @property
    def header_name(self) -> str:
        return f"{Path(self.file_name).stem}.header.csv"


_STRING = pa.string()
_INT64 = pa.int64()
_UTC_TIMESTAMP = pa.timestamp("us", tz="UTC")

IMPORT_DEFINITIONS: tuple[_ImportDefinition, ...] = (
    _ImportDefinition(
        "transaction_nodes.parquet",
        "transactions",
        "node",
        pa.schema(
            [
                pa.field("txid", _STRING, nullable=False),
                pa.field("fee_sats", _INT64, nullable=False),
                pa.field("script_type", _STRING),
            ]
        ),
        """SELECT txid::VARCHAR AS txid, fee_sats::BIGINT AS fee_sats,
        script_type::VARCHAR AS script_type FROM transactions ORDER BY txid""",
        "txid:ID(Transaction),fee_sats,script_type\ntxid,fee_sats,script_type\n",
    ),
    _ImportDefinition(
        "address_nodes.parquet",
        "addresses",
        "node",
        pa.schema([pa.field("address", _STRING, nullable=False)]),
        """SELECT address::VARCHAR AS address FROM (
        SELECT address FROM transaction_inputs
        UNION
        SELECT address FROM transaction_outputs
        ) ORDER BY address""",
        "address:ID(Address)\naddress\n",
    ),
    _ImportDefinition(
        "ip_address_nodes.parquet",
        "ip_addresses",
        "node",
        pa.schema([pa.field("ip", _STRING, nullable=False)]),
        """SELECT ip::VARCHAR AS ip FROM (
        SELECT src_ip AS ip FROM network_observations
        UNION
        SELECT dst_ip AS ip FROM network_observations
        ) ORDER BY ip""",
        "ip:ID(IPAddress)\nip\n",
    ),
    _ImportDefinition(
        "network_observation_nodes.parquet",
        "network_observations",
        "node",
        pa.schema(
            [
                pa.field("observation_id", _STRING, nullable=False),
                pa.field("observed_at", _UTC_TIMESTAMP, nullable=False),
                pa.field("src_port", _INT64, nullable=False),
                pa.field("dst_port", _INT64, nullable=False),
                pa.field("reported_geo_country", _STRING),
                pa.field("reported_asn", _INT64),
                pa.field("source_record_id", _STRING, nullable=False),
            ]
        ),
        """SELECT observation_id::VARCHAR AS observation_id,
        observed_at::TIMESTAMPTZ AS observed_at, src_port::BIGINT AS src_port,
        dst_port::BIGINT AS dst_port, reported_geo_country::VARCHAR AS reported_geo_country,
        reported_asn::BIGINT AS reported_asn, source_record_id::VARCHAR AS source_record_id
        FROM network_observations ORDER BY observation_id""",
        (
            "observation_id:ID(NetworkObservation),observed_at,src_port,dst_port,"
            "reported_geo_country,reported_asn,source_record_id\n"
            "observation_id,observed_at,src_port,dst_port,reported_geo_country,reported_asn,"
            "source_record_id\n"
        ),
    ),
    _ImportDefinition(
        "spent_in_relationships.parquet",
        "spent_in",
        "relationship",
        pa.schema(
            [
                pa.field("address", _STRING, nullable=False),
                pa.field("txid", _STRING, nullable=False),
                pa.field("input_index", _INT64, nullable=False),
                pa.field("amount_sats", _INT64, nullable=False),
            ]
        ),
        """SELECT address::VARCHAR AS address, txid::VARCHAR AS txid,
        input_index::BIGINT AS input_index, amount_sats::BIGINT AS amount_sats
        FROM transaction_inputs ORDER BY txid, input_index, address""",
        (
            ":START_ID(Address),:END_ID(Transaction),input_index,amount_sats\n"
            "address,txid,input_index,amount_sats\n"
        ),
    ),
    _ImportDefinition(
        "created_output_relationships.parquet",
        "created_output",
        "relationship",
        pa.schema(
            [
                pa.field("txid", _STRING, nullable=False),
                pa.field("address", _STRING, nullable=False),
                pa.field("output_index", _INT64, nullable=False),
                pa.field("amount_sats", _INT64, nullable=False),
            ]
        ),
        """SELECT txid::VARCHAR AS txid, address::VARCHAR AS address,
        output_index::BIGINT AS output_index, amount_sats::BIGINT AS amount_sats
        FROM transaction_outputs ORDER BY txid, output_index, address""",
        (
            ":START_ID(Transaction),:END_ID(Address),output_index,amount_sats\n"
            "txid,address,output_index,amount_sats\n"
        ),
    ),
    _ImportDefinition(
        "observed_transaction_relationships.parquet",
        "observed_transaction",
        "relationship",
        pa.schema(
            [
                pa.field("observation_id", _STRING, nullable=False),
                pa.field("txid", _STRING, nullable=False),
            ]
        ),
        """SELECT observation_id::VARCHAR AS observation_id, txid::VARCHAR AS txid
        FROM network_observations ORDER BY observation_id""",
        ":START_ID(NetworkObservation),:END_ID(Transaction)\nobservation_id,txid\n",
    ),
    _ImportDefinition(
        "source_ip_relationships.parquet",
        "source_ip",
        "relationship",
        pa.schema(
            [
                pa.field("observation_id", _STRING, nullable=False),
                pa.field("ip", _STRING, nullable=False),
            ]
        ),
        """SELECT observation_id::VARCHAR AS observation_id, src_ip::VARCHAR AS ip
        FROM network_observations ORDER BY observation_id""",
        ":START_ID(NetworkObservation),:END_ID(IPAddress)\nobservation_id,ip\n",
    ),
    _ImportDefinition(
        "destination_ip_relationships.parquet",
        "destination_ip",
        "relationship",
        pa.schema(
            [
                pa.field("observation_id", _STRING, nullable=False),
                pa.field("ip", _STRING, nullable=False),
            ]
        ),
        """SELECT observation_id::VARCHAR AS observation_id, dst_ip::VARCHAR AS ip
        FROM network_observations ORDER BY observation_id""",
        ":START_ID(NetworkObservation),:END_ID(IPAddress)\nobservation_id,ip\n",
    ),
)

_DEFINITION_BY_FILE = {definition.file_name: definition for definition in IMPORT_DEFINITIONS}


def prepare_graph_import(dataset_path: Path, output_path: Path) -> PreparedGraphImport:
    dataset = AnalyticalDataset(dataset_path)
    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise GraphImportError(f"graph-import destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise GraphImportError(
            f"graph-import destination parent does not exist: {destination.parent}"
        )

    staging = destination.parent / f".{destination.name}.staging-{uuid4().hex}"
    try:
        staging.mkdir()
        (staging / "headers").mkdir()
        with dataset.connect() as connection:
            files = _write_import_files(connection, staging)
        _write_manifest(dataset, staging, files)
        report = validate_graph_import(staging, canonical_dataset=dataset)
        if not report.is_valid:
            codes = ", ".join(issue.code for issue in report.issues)
            raise GraphImportError(f"prepared graph-import dataset failed validation: {codes}")
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    prepared = PreparedGraphImport(destination, _load_manifest(destination))
    _LOGGER.info(
        "graph import prepared: output=%s nodes=%d relationships=%d graph_schema=%s",
        destination,
        sum(prepared.manifest.node_counts.values()),
        sum(prepared.manifest.relationship_counts.values()),
        prepared.manifest.graph_schema_version,
    )
    return prepared


def validate_graph_import(
    input_path: Path,
    *,
    canonical_dataset: AnalyticalDataset | None = None,
) -> GraphImportValidationReport:
    root = _resolve_directory(input_path)
    manifest = _load_manifest(root)
    issues: list[GraphImportIssue] = []

    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        for definition in IMPORT_DEFINITIONS:
            file = manifest.files[definition.file_name]
            _validate_file_metadata(definition, file, issues)
            connection.read_parquet(str(file.path)).create_view(_view_name(definition))
        _validate_import_relations(connection, issues)
        if canonical_dataset is not None:
            _validate_against_canonical(connection, canonical_dataset, manifest, issues)
    except duckdb.Error as error:
        raise GraphImportError(f"failed to validate graph-import relations: {error}") from error
    finally:
        connection.close()
    return GraphImportValidationReport(tuple(issues))


def load_graph_import_manifest(input_path: Path) -> GraphImportManifest:
    return _load_manifest(_resolve_directory(input_path))


def _write_import_files(
    connection: duckdb.DuckDBPyConnection, root: Path
) -> dict[str, GraphImportFile]:
    result: dict[str, GraphImportFile] = {}
    for definition in IMPORT_DEFINITIONS:
        path = root / definition.file_name
        header_path = root / "headers" / definition.header_name
        header_path.write_text(definition.header, encoding="utf-8", newline="\n")
        reader = connection.sql(definition.query).arrow(batch_size=_ROW_GROUP_SIZE)
        rows = 0
        with pq.ParquetWriter(
            path,
            definition.schema,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
            version="2.6",
        ) as writer:
            for batch in reader:
                table = pa.Table.from_batches([batch]).cast(definition.schema, safe=True)
                writer.write_table(table, row_group_size=_ROW_GROUP_SIZE)
                rows += table.num_rows
        result[definition.file_name] = GraphImportFile(
            path=path,
            header_path=header_path,
            rows=rows,
            bytes=path.stat().st_size,
            sha256=_sha256_file(path),
            header_sha256=_sha256_file(header_path),
        )
    return result


def _write_manifest(
    dataset: AnalyticalDataset,
    root: Path,
    files: Mapping[str, GraphImportFile],
) -> GraphImportManifest:
    node_counts = {
        definition.count_name: files[definition.file_name].rows
        for definition in IMPORT_DEFINITIONS
        if definition.kind == "node"
    }
    relationship_counts = {
        definition.count_name: files[definition.file_name].rows
        for definition in IMPORT_DEFINITIONS
        if definition.kind == "relationship"
    }
    built_at = datetime.now(UTC)
    canonical_manifest_sha256 = _sha256_file(dataset.path / "manifest.json")
    raw_files = {
        name: {
            "file": file.path.relative_to(root).as_posix(),
            "header": file.header_path.relative_to(root).as_posix(),
            "rows": file.rows,
            "bytes": file.bytes,
            "sha256": file.sha256,
            "header_sha256": file.header_sha256,
        }
        for name, file in files.items()
    }
    raw_manifest = {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "canonical_schema_version": dataset.manifest.schema_version,
        "canonical_manifest_sha256": canonical_manifest_sha256,
        "neo4j_version": NEO4J_VERSION,
        "built_at": built_at.isoformat().replace("+00:00", "Z"),
        "node_counts": node_counts,
        "relationship_counts": relationship_counts,
        "files": raw_files,
    }
    (root / "graph-manifest.json").write_text(
        json.dumps(raw_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return GraphImportManifest(
        graph_schema_version=GRAPH_SCHEMA_VERSION,
        canonical_schema_version=dataset.manifest.schema_version,
        canonical_manifest_sha256=canonical_manifest_sha256,
        neo4j_version=NEO4J_VERSION,
        built_at=built_at,
        node_counts=node_counts,
        relationship_counts=relationship_counts,
        files=dict(files),
    )


def _load_manifest(root: Path) -> GraphImportManifest:
    manifest_path = root / "graph-manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GraphImportError(f"graph-build manifest is missing: {manifest_path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GraphImportError(f"graph-build manifest is malformed: {error}") from error
    if not isinstance(raw, dict):
        raise GraphImportError("graph-build manifest must be a JSON object")

    version = raw.get("graph_schema_version")
    if not isinstance(version, str) or version not in SUPPORTED_GRAPH_SCHEMA_VERSIONS:
        raise GraphImportError(f"unsupported graph schema version: {version!r}")
    canonical_version = _required_string(raw, "canonical_schema_version")
    canonical_hash = _sha256_value(raw.get("canonical_manifest_sha256"), "canonical manifest")
    neo4j_version = _required_string(raw, "neo4j_version")
    if neo4j_version != NEO4J_VERSION:
        raise GraphImportError(
            f"graph import targets Neo4j {neo4j_version}; expected {NEO4J_VERSION}"
        )
    built_at = _parse_utc_timestamp(raw.get("built_at"))
    node_counts = _count_mapping(raw.get("node_counts"), NODE_COUNTS, "node_counts")
    relationship_counts = _count_mapping(
        raw.get("relationship_counts"), RELATIONSHIP_COUNTS, "relationship_counts"
    )
    raw_files = raw.get("files")
    if not isinstance(raw_files, dict) or set(raw_files) != set(_DEFINITION_BY_FILE):
        raise GraphImportError("graph-build manifest files do not match the import contract")

    files: dict[str, GraphImportFile] = {}
    for name, definition in _DEFINITION_BY_FILE.items():
        item = raw_files.get(name)
        if not isinstance(item, dict):
            raise GraphImportError(f"manifest file entry must be an object: {name}")
        expected_header = Path("headers") / definition.header_name
        if item.get("file") != name or item.get("header") != expected_header.as_posix():
            raise GraphImportError(f"manifest paths do not match the import contract: {name}")
        file_path = _resolve_child_file(root, Path(name))
        header_path = _resolve_child_file(root, expected_header)
        rows = _nonnegative_integer(item.get("rows"), f"row count for {name}")
        bytes_count = _nonnegative_integer(item.get("bytes"), f"byte count for {name}")
        files[name] = GraphImportFile(
            path=file_path,
            header_path=header_path,
            rows=rows,
            bytes=bytes_count,
            sha256=_sha256_value(item.get("sha256"), name),
            header_sha256=_sha256_value(item.get("header_sha256"), definition.header_name),
        )
    return GraphImportManifest(
        graph_schema_version=version,
        canonical_schema_version=canonical_version,
        canonical_manifest_sha256=canonical_hash,
        neo4j_version=neo4j_version,
        built_at=built_at,
        node_counts=node_counts,
        relationship_counts=relationship_counts,
        files=files,
    )


def _validate_file_metadata(
    definition: _ImportDefinition,
    file: GraphImportFile,
    issues: list[GraphImportIssue],
) -> None:
    try:
        actual_schema = pq.read_schema(file.path)
        actual_rows = pq.ParquetFile(file.path).metadata.num_rows
    except (OSError, ValueError) as error:
        raise GraphImportError(f"cannot read {definition.file_name}: {error}") from error
    _append_mismatch(
        issues,
        "PARQUET_SCHEMA_MISMATCH",
        not actual_schema.equals(definition.schema, check_metadata=False),
        f"{definition.file_name} does not match its explicit Arrow schema",
    )
    _append_mismatch(
        issues,
        "PARQUET_ROW_COUNT_MISMATCH",
        actual_rows != file.rows,
        f"{definition.file_name} contains {actual_rows} rows; manifest declares {file.rows}",
        abs(actual_rows - file.rows),
    )
    _append_mismatch(
        issues,
        "PARQUET_SIZE_MISMATCH",
        file.path.stat().st_size != file.bytes,
        f"{definition.file_name} byte size differs from its manifest",
    )
    _append_mismatch(
        issues,
        "PARQUET_HASH_MISMATCH",
        _sha256_file(file.path) != file.sha256,
        f"{definition.file_name} SHA-256 differs from its manifest",
    )
    _append_mismatch(
        issues,
        "HEADER_CONTENT_MISMATCH",
        file.header_path.read_text(encoding="utf-8") != definition.header,
        f"{definition.header_name} does not match the Neo4j import header contract",
    )
    _append_mismatch(
        issues,
        "HEADER_HASH_MISMATCH",
        _sha256_file(file.header_path) != file.header_sha256,
        f"{definition.header_name} SHA-256 differs from its manifest",
    )


def _validate_import_relations(
    connection: duckdb.DuckDBPyConnection, issues: list[GraphImportIssue]
) -> None:
    checks = (
        (
            "DUPLICATE_TRANSACTION_ID",
            "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM graph_transactions "
            "GROUP BY txid HAVING count(*) > 1)",
            "transaction node IDs are not unique",
        ),
        (
            "DUPLICATE_ADDRESS_ID",
            "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM graph_addresses "
            "GROUP BY address HAVING count(*) > 1)",
            "address node IDs are not unique",
        ),
        (
            "DUPLICATE_IP_ID",
            "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM graph_ip_addresses "
            "GROUP BY ip HAVING count(*) > 1)",
            "IP address node IDs are not unique",
        ),
        (
            "DUPLICATE_OBSERVATION_ID",
            "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM graph_observations "
            "GROUP BY observation_id HAVING count(*) > 1)",
            "network observation node IDs are not unique",
        ),
        (
            "ORPHAN_SPENT_IN_ADDRESS",
            "SELECT count(*) FROM graph_spent_in r LEFT JOIN graph_addresses n USING (address) "
            "WHERE n.address IS NULL",
            "SPENT_IN relationships reference missing Address nodes",
        ),
        (
            "ORPHAN_SPENT_IN_TRANSACTION",
            "SELECT count(*) FROM graph_spent_in r LEFT JOIN graph_transactions n USING (txid) "
            "WHERE n.txid IS NULL",
            "SPENT_IN relationships reference missing Transaction nodes",
        ),
        (
            "ORPHAN_CREATED_OUTPUT_TRANSACTION",
            "SELECT count(*) FROM graph_created_output r LEFT JOIN graph_transactions n "
            "USING (txid) WHERE n.txid IS NULL",
            "CREATED_OUTPUT relationships reference missing Transaction nodes",
        ),
        (
            "ORPHAN_CREATED_OUTPUT_ADDRESS",
            "SELECT count(*) FROM graph_created_output r LEFT JOIN graph_addresses n "
            "USING (address) WHERE n.address IS NULL",
            "CREATED_OUTPUT relationships reference missing Address nodes",
        ),
        (
            "ORPHAN_OBSERVED_TRANSACTION_OBSERVATION",
            "SELECT count(*) FROM graph_observed_transaction r LEFT JOIN graph_observations n "
            "USING (observation_id) WHERE n.observation_id IS NULL",
            "OBSERVED_TRANSACTION relationships reference missing observations",
        ),
        (
            "ORPHAN_OBSERVED_TRANSACTION_TRANSACTION",
            "SELECT count(*) FROM graph_observed_transaction r LEFT JOIN graph_transactions n "
            "USING (txid) WHERE n.txid IS NULL",
            "OBSERVED_TRANSACTION relationships reference missing transactions",
        ),
        (
            "ORPHAN_SOURCE_IP_OBSERVATION",
            "SELECT count(*) FROM graph_source_ip r LEFT JOIN graph_observations n "
            "USING (observation_id) WHERE n.observation_id IS NULL",
            "SOURCE_IP relationships reference missing observations",
        ),
        (
            "ORPHAN_SOURCE_IP_ADDRESS",
            "SELECT count(*) FROM graph_source_ip r LEFT JOIN graph_ip_addresses n USING (ip) "
            "WHERE n.ip IS NULL",
            "SOURCE_IP relationships reference missing IPAddress nodes",
        ),
        (
            "ORPHAN_DESTINATION_IP_OBSERVATION",
            "SELECT count(*) FROM graph_destination_ip r LEFT JOIN graph_observations n "
            "USING (observation_id) WHERE n.observation_id IS NULL",
            "DESTINATION_IP relationships reference missing observations",
        ),
        (
            "ORPHAN_DESTINATION_IP_ADDRESS",
            "SELECT count(*) FROM graph_destination_ip r LEFT JOIN graph_ip_addresses n USING (ip) "
            "WHERE n.ip IS NULL",
            "DESTINATION_IP relationships reference missing IPAddress nodes",
        ),
        (
            "INVALID_OBSERVATION_RELATIONSHIP_CARDINALITY",
            """SELECT count(*) FROM graph_observations o
            LEFT JOIN (SELECT observation_id, count(*) n FROM graph_observed_transaction
            GROUP BY observation_id) t USING (observation_id)
            LEFT JOIN (SELECT observation_id, count(*) n FROM graph_source_ip
            GROUP BY observation_id) s USING (observation_id)
            LEFT JOIN (SELECT observation_id, count(*) n FROM graph_destination_ip
            GROUP BY observation_id) d USING (observation_id)
            WHERE coalesce(t.n, 0) <> 1 OR coalesce(s.n, 0) <> 1 OR coalesce(d.n, 0) <> 1""",
            "each observation must have exactly one transaction, source IP, and destination IP",
        ),
    )
    for code, sql, message in checks:
        row = connection.execute(sql).fetchone()
        if row is None:
            raise AssertionError(f"graph import check {code} returned no row")
        count = int(row[0])
        if count:
            issues.append(GraphImportIssue(code, count, message))


def _validate_against_canonical(
    connection: duckdb.DuckDBPyConnection,
    dataset: AnalyticalDataset,
    manifest: GraphImportManifest,
    issues: list[GraphImportIssue],
) -> None:
    _append_mismatch(
        issues,
        "CANONICAL_SCHEMA_VERSION_MISMATCH",
        manifest.canonical_schema_version != dataset.manifest.schema_version,
        "graph import canonical schema version does not match the supplied dataset",
    )
    _append_mismatch(
        issues,
        "CANONICAL_MANIFEST_HASH_MISMATCH",
        manifest.canonical_manifest_sha256 != _sha256_file(dataset.path / "manifest.json"),
        "graph import was not derived from the supplied canonical manifest",
    )
    for table_name, table in dataset.manifest.tables.items():
        connection.read_parquet(str(table.path)).create_view(table_name)

    comparisons = (
        (
            "TRANSACTION_VALUE_MISMATCH",
            "graph_transactions",
            "SELECT txid, fee_sats, script_type FROM transactions",
        ),
        (
            "ADDRESS_SET_MISMATCH",
            "graph_addresses",
            "SELECT address FROM transaction_inputs UNION SELECT address FROM transaction_outputs",
        ),
        (
            "IP_SET_MISMATCH",
            "graph_ip_addresses",
            """SELECT src_ip AS ip FROM network_observations
            UNION SELECT dst_ip AS ip FROM network_observations""",
        ),
        (
            "OBSERVATION_VALUE_MISMATCH",
            "graph_observations",
            """SELECT observation_id, observed_at, src_port, dst_port, reported_geo_country,
            reported_asn, source_record_id FROM network_observations""",
        ),
        (
            "SPENT_IN_VALUE_MISMATCH",
            "graph_spent_in",
            "SELECT address, txid, input_index, amount_sats FROM transaction_inputs",
        ),
        (
            "CREATED_OUTPUT_VALUE_MISMATCH",
            "graph_created_output",
            "SELECT txid, address, output_index, amount_sats FROM transaction_outputs",
        ),
        (
            "OBSERVED_TRANSACTION_VALUE_MISMATCH",
            "graph_observed_transaction",
            "SELECT observation_id, txid FROM network_observations",
        ),
        (
            "SOURCE_IP_VALUE_MISMATCH",
            "graph_source_ip",
            "SELECT observation_id, src_ip AS ip FROM network_observations",
        ),
        (
            "DESTINATION_IP_VALUE_MISMATCH",
            "graph_destination_ip",
            "SELECT observation_id, dst_ip AS ip FROM network_observations",
        ),
    )
    for code, graph_view, canonical_query in comparisons:
        sql = f"""WITH expected AS ({canonical_query}),
        actual AS (SELECT * FROM {graph_view})
        SELECT count(*) FROM (
        (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
        )"""
        row = connection.execute(sql).fetchone()
        if row is None:
            raise AssertionError(f"canonical comparison {code} returned no row")
        count = int(row[0])
        if count:
            issues.append(GraphImportIssue(code, count, "derived rows differ from canonical data"))

    provenance = connection.execute(
        """SELECT count(*) FROM graph_observations o
        LEFT JOIN source_records s USING (source_record_id)
        WHERE s.source_record_id IS NULL"""
    ).fetchone()
    if provenance is None:
        raise AssertionError("graph provenance validation returned no row")
    if int(provenance[0]):
        issues.append(
            GraphImportIssue(
                "ORPHAN_OBSERVATION_PROVENANCE",
                int(provenance[0]),
                "observation source_record_id is absent from canonical source_records",
            )
        )


def _view_name(definition: _ImportDefinition) -> str:
    if definition.count_name == "network_observations":
        return "graph_observations"
    return f"graph_{definition.count_name}"


def _resolve_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise GraphImportError(f"graph-import dataset does not exist: {path}") from error
    if not resolved.is_dir():
        raise GraphImportError(f"graph-import dataset is not a directory: {resolved}")
    return resolved


def _resolve_child_file(root: Path, relative_path: Path) -> Path:
    try:
        resolved = (root / relative_path).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise GraphImportError(
            f"graph-import file is missing or escapes its root: {relative_path}"
        ) from error
    if not resolved.is_file():
        raise GraphImportError(f"graph-import path is not a file: {relative_path}")
    return resolved


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise GraphImportError(f"graph-build manifest {key} must be a non-empty string")
    return value


def _count_mapping(value: Any, names: tuple[str, ...], field: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(names):
        raise GraphImportError(f"graph-build manifest {field} does not match the graph contract")
    return {name: _nonnegative_integer(value.get(name), f"{field}.{name}") for name in names}


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GraphImportError(f"graph-build manifest {field} must be a non-negative integer")
    return int(value)


def _sha256_value(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GraphImportError(f"graph-build manifest SHA-256 is invalid: {field}")
    return value


def _parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GraphImportError("graph-build manifest built_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GraphImportError("graph-build manifest built_at is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GraphImportError("graph-build manifest built_at must include a timezone")
    return parsed.astimezone(UTC)


def _append_mismatch(
    issues: list[GraphImportIssue],
    code: str,
    condition: bool,
    message: str,
    count: int = 1,
) -> None:
    if condition:
        issues.append(GraphImportIssue(code, max(count, 1), message))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
