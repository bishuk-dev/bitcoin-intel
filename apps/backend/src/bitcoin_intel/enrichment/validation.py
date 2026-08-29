from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.enrichment.models import (
    ENRICHMENT_SCHEMA_VERSION,
    IP_ENRICHMENT_SCHEMA,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    TABLE_NAME,
    EnrichmentValidationIssue,
    EnrichmentValidationReport,
)
from bitcoin_intel.enrichment.resources import (
    ATTRIBUTION,
    LICENSE_IDENTIFIER,
    PROVIDER,
    PROVIDER_URL,
)


class EnrichmentStoreError(RuntimeError):
    """Raised when enrichment metadata cannot be interpreted safely."""


def validate_enrichment_store(
    enrichment_path: Path, dataset_path: Path
) -> EnrichmentValidationReport:
    root = _resolve_directory(enrichment_path)
    dataset = AnalyticalDataset(dataset_path)
    manifest = _load_manifest(root)
    issues: list[EnrichmentValidationIssue] = []
    _mismatch(
        issues,
        "ENRICHMENT_SCHEMA_VERSION_MISMATCH",
        manifest.get("enrichment_schema_version") != ENRICHMENT_SCHEMA_VERSION,
        "enrichment schema version is unsupported",
    )
    canonical_hash = _sha256_file(dataset.path / "manifest.json")
    _mismatch(
        issues,
        "CANONICAL_MANIFEST_HASH_MISMATCH",
        manifest.get("canonical_manifest_sha256") != canonical_hash,
        "enrichment store was not derived from the supplied canonical manifest",
    )
    _mismatch(
        issues,
        "CANONICAL_SCHEMA_VERSION_MISMATCH",
        manifest.get("canonical_schema_version") != dataset.manifest.schema_version,
        "canonical schema version differs from the supplied dataset",
    )
    resources = manifest.get("resources")
    if not isinstance(resources, dict) or set(resources) != {"country", "asn"}:
        raise EnrichmentStoreError("manifest resources must declare country and ASN databases")
    for role in ("country", "asn"):
        resource = resources[role]
        if not isinstance(resource, dict):
            raise EnrichmentStoreError(f"manifest {role} resource metadata is malformed")
        required = {
            "provider",
            "role",
            "database_type",
            "release",
            "build_epoch",
            "ip_version",
            "file_name",
            "bytes",
            "sha256",
            "license",
            "attribution",
            "provider_url",
        }
        if set(resource) != required or resource.get("role") != role:
            raise EnrichmentStoreError(f"manifest {role} resource metadata is incomplete")
        database_type = resource.get("database_type")
        if (
            resource.get("provider") != PROVIDER
            or resource.get("license") != LICENSE_IDENTIFIER
            or resource.get("attribution") != ATTRIBUTION
            or resource.get("provider_url") != PROVIDER_URL
            or not isinstance(database_type, str)
            or "dbip" not in database_type.lower()
            or role not in database_type.lower()
        ):
            raise EnrichmentStoreError(f"manifest {role} resource contract is unsupported")
        release = resource.get("release")
        if release is not None and (not isinstance(release, str) or not release.strip()):
            raise EnrichmentStoreError(f"manifest {role} resource release is invalid")
        if not _nonnegative_int(resource.get("build_epoch")) or resource.get("ip_version") not in {
            4,
            6,
        }:
            raise EnrichmentStoreError(f"manifest {role} MMDB metadata is invalid")
        if not isinstance(resource.get("file_name"), str) or not resource["file_name"]:
            raise EnrichmentStoreError(f"manifest {role} resource filename is invalid")
        if not _valid_hash(resource.get("sha256")) or not _positive_int(resource.get("bytes")):
            raise EnrichmentStoreError(f"manifest {role} resource identity is invalid")
    expected_configuration = {
        "lookup_mode": "offline",
        "one_row_per_distinct_canonical_ip": True,
        "missing_lookup_value": None,
    }
    if manifest.get("build_configuration") != expected_configuration:
        raise EnrichmentStoreError("manifest build_configuration is unsupported")
    semantic_identity = {
        key: manifest.get(key)
        for key in (
            "canonical_manifest_sha256",
            "canonical_schema_version",
            "enrichment_schema_version",
            "resources",
            "build_configuration",
        )
    }
    expected_id = hashlib.sha256(
        json.dumps(semantic_identity, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    _mismatch(
        issues,
        "ENRICHMENT_DATASET_ID_MISMATCH",
        manifest.get("enrichment_dataset_id") != expected_id,
        "enrichment dataset semantic identity is invalid",
    )
    raw_tables = manifest.get("output_tables")
    if not isinstance(raw_tables, dict) or set(raw_tables) != {TABLE_NAME}:
        raise EnrichmentStoreError("manifest output_tables does not match the enrichment contract")
    raw_table = raw_tables[TABLE_NAME]
    if not isinstance(raw_table, dict):
        raise EnrichmentStoreError("manifest enrichment table metadata is malformed")
    relative = Path(TABLE_NAME) / PART_FILE_NAME
    if raw_table.get("file") != relative.as_posix():
        raise EnrichmentStoreError("manifest enrichment table path is unsupported")
    table_path = _resolve_child(root, relative)
    try:
        parquet = pq.ParquetFile(table_path)
        actual_schema = parquet.schema_arrow
        actual_rows = parquet.metadata.num_rows
    except (OSError, ValueError) as error:
        issues.append(EnrichmentValidationIssue("UNREADABLE_ENRICHMENT_PARQUET", 1, str(error)))
        return EnrichmentValidationReport(tuple(issues))
    schema_matches = actual_schema.equals(IP_ENRICHMENT_SCHEMA, check_metadata=False)
    _mismatch(
        issues,
        "ENRICHMENT_SCHEMA_MISMATCH",
        not schema_matches,
        "ip_enrichment has an unexpected Parquet schema",
    )
    _mismatch(
        issues,
        "ENRICHMENT_ROW_COUNT_MISMATCH",
        raw_table.get("rows") != actual_rows,
        "enrichment row count differs from the manifest",
    )
    _mismatch(
        issues,
        "ENRICHMENT_FILE_SIZE_MISMATCH",
        raw_table.get("bytes") != table_path.stat().st_size,
        "enrichment byte size differs from the manifest",
    )
    _mismatch(
        issues,
        "ENRICHMENT_FILE_HASH_MISMATCH",
        raw_table.get("sha256") != _sha256_file(table_path),
        "enrichment SHA-256 differs from the manifest",
    )
    if schema_matches:
        _validate_values(table_path, dataset, manifest, issues)
    return EnrichmentValidationReport(tuple(issues))


def _validate_values(
    table_path: Path,
    dataset: AnalyticalDataset,
    manifest: dict[str, Any],
    issues: list[EnrichmentValidationIssue],
) -> None:
    with dataset.connect() as connection:
        connection.read_parquet(str(table_path)).create_view(TABLE_NAME)
        checks = (
            (
                "DUPLICATE_ENRICHMENT_IP",
                "SELECT count(*)-count(DISTINCT ip) FROM ip_enrichment",
                "enrichment IP identities are not unique",
            ),
            (
                "CANONICAL_IP_SET_MISMATCH",
                """SELECT count(*) FROM (
                (SELECT ip FROM ip_enrichment EXCEPT SELECT src_ip FROM network_observations
                 EXCEPT SELECT dst_ip FROM network_observations)
                UNION ALL
                ((SELECT src_ip AS ip FROM network_observations
                  UNION SELECT dst_ip AS ip FROM network_observations)
                 EXCEPT SELECT ip FROM ip_enrichment))""",
                "enrichment identities differ from distinct canonical endpoints",
            ),
            (
                "INVALID_COUNTRY_VALUE",
                """SELECT count(*) FROM ip_enrichment
                WHERE enriched_country_code IS NOT NULL AND
                NOT regexp_full_match(enriched_country_code, '[A-Z]{2}')""",
                "enriched country codes must be uppercase two-letter values",
            ),
            (
                "INVALID_ASN_VALUE",
                """SELECT count(*) FROM ip_enrichment
                WHERE enriched_asn IS NOT NULL AND enriched_asn <= 0""",
                "enriched ASNs must be positive integers",
            ),
            (
                "COUNTRY_FOUND_INCONSISTENT",
                """SELECT count(*) FROM ip_enrichment
                WHERE country_found <> (enriched_country_code IS NOT NULL)""",
                "country found flags do not match values",
            ),
            (
                "ASN_FOUND_INCONSISTENT",
                """SELECT count(*) FROM ip_enrichment WHERE asn_found <>
                (enriched_asn IS NOT NULL OR enriched_as_org IS NOT NULL)""",
                "ASN found flags do not match values",
            ),
        )
        for code, sql, message in checks:
            _query_issue(connection, issues, code, sql, message)
        row = connection.execute(
            """SELECT count(*), count(*) FILTER (WHERE country_found),
            count(*) FILTER (WHERE asn_found) FROM ip_enrichment"""
        ).fetchone()
        if row is None:
            raise AssertionError("enrichment validation summary returned no row")
        lookup_counts = manifest.get("lookup_counts")
        if not isinstance(lookup_counts, dict):
            raise EnrichmentStoreError("manifest lookup_counts is malformed")
        expected = {
            "distinct_ips": int(row[0]),
            "country_lookups": int(row[0]),
            "country_found": int(row[1]),
            "asn_lookups": int(row[0]),
            "asn_found": int(row[2]),
        }
        _mismatch(
            issues,
            "LOOKUP_COUNT_MISMATCH",
            lookup_counts != expected,
            "lookup counters differ from enrichment values",
        )
        for row in connection.execute(
            """SELECT ip, country_network, asn_network,
            is_private, is_global, is_loopback, is_link_local, is_multicast, is_reserved
            FROM ip_enrichment"""
        ).fetchall():
            ip, country_network, asn_network, *flags = row
            try:
                address = ipaddress.ip_address(ip)
                if address.compressed != ip:
                    raise ValueError("IP is not in canonical textual form")
                if country_network is not None and address not in ipaddress.ip_network(
                    country_network
                ):
                    raise ValueError("country network does not contain IP")
                if asn_network is not None and address not in ipaddress.ip_network(asn_network):
                    raise ValueError("ASN network does not contain IP")
                expected_flags = [
                    address.is_private,
                    address.is_global,
                    address.is_loopback,
                    address.is_link_local,
                    address.is_multicast,
                    address.is_reserved,
                ]
                if flags != expected_flags:
                    raise ValueError("IP classification flags are inconsistent")
            except ValueError:
                issues.append(
                    EnrichmentValidationIssue(
                        "INVALID_IP_OR_NETWORK", 1, "an enrichment IP/network value is invalid"
                    )
                )


def _query_issue(
    connection: duckdb.DuckDBPyConnection,
    issues: list[EnrichmentValidationIssue],
    code: str,
    sql: str,
    message: str,
) -> None:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise AssertionError(f"enrichment validation check returned no row: {code}")
    count = int(row[0])
    if count:
        issues.append(EnrichmentValidationIssue(code, count, message))


def _load_manifest(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnrichmentStoreError(
            f"enrichment manifest is unreadable or malformed: {error}"
        ) from error
    if not isinstance(value, dict):
        raise EnrichmentStoreError("enrichment manifest must be a JSON object")
    return value


def _resolve_directory(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise EnrichmentStoreError(f"enrichment store does not exist: {path}") from error
    if not root.is_dir():
        raise EnrichmentStoreError(f"enrichment store is not a directory: {root}")
    return root


def _resolve_child(root: Path, relative: Path) -> Path:
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise EnrichmentStoreError(
            f"enrichment file is missing or escapes its root: {relative}"
        ) from error
    if not path.is_file():
        raise EnrichmentStoreError(f"enrichment path is not a file: {relative}")
    return path


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _mismatch(
    issues: list[EnrichmentValidationIssue], code: str, condition: bool, message: str
) -> None:
    if condition:
        issues.append(EnrichmentValidationIssue(code, 1, message))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
