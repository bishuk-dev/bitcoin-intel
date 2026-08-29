from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.analytics.validation import validate_analytical_dataset
from bitcoin_intel.enrichment.models import (
    ENRICHMENT_SCHEMA_VERSION,
    IP_ENRICHMENT_SCHEMA,
    MANIFEST_FILE_NAME,
    PART_FILE_NAME,
    TABLE_NAME,
    EnrichmentBuildSummary,
)
from bitcoin_intel.enrichment.resources import OfflineGeoIPReaders

_LOGGER = logging.getLogger(__name__)
_ROW_GROUP_SIZE = 65_536


class EnrichmentBuildError(RuntimeError):
    """Raised when an enrichment store cannot be built or published safely."""


def build_ip_enrichment(
    dataset_path: Path,
    output_path: Path,
    country_db: Path,
    asn_db: Path,
) -> EnrichmentBuildSummary:
    dataset = AnalyticalDataset(dataset_path)
    integrity = validate_analytical_dataset(dataset)
    if not integrity.is_valid:
        codes = ", ".join(issue.code for issue in integrity.issues)
        raise EnrichmentBuildError(f"canonical dataset failed integrity validation: {codes}")
    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise EnrichmentBuildError(
            f"enrichment output already exists and will not be overwritten: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        table_path = staging / TABLE_NAME / PART_FILE_NAME
        table_path.parent.mkdir(parents=True, exist_ok=False)
        with OfflineGeoIPReaders(country_db, asn_db) as resources:
            row_count, country_found, asn_found = _write_rows(dataset, resources, table_path)
            resource_metadata = {
                "country": resources.country_descriptor.manifest_value(),
                "asn": resources.asn_descriptor.manifest_value(),
            }
        canonical_hash = _sha256_file(dataset.path / "manifest.json")
        build_configuration = {
            "lookup_mode": "offline",
            "one_row_per_distinct_canonical_ip": True,
            "missing_lookup_value": None,
        }
        semantic_identity = {
            "canonical_manifest_sha256": canonical_hash,
            "canonical_schema_version": dataset.manifest.schema_version,
            "enrichment_schema_version": ENRICHMENT_SCHEMA_VERSION,
            "resources": resource_metadata,
            "build_configuration": build_configuration,
        }
        enrichment_dataset_id = _sha256_bytes(_canonical_json(semantic_identity))
        manifest = {
            "enrichment_dataset_id": enrichment_dataset_id,
            **semantic_identity,
            "built_at": _timestamp(datetime.now(UTC)),
            "output_tables": {
                TABLE_NAME: {
                    "file": f"{TABLE_NAME}/{PART_FILE_NAME}",
                    "rows": row_count,
                    "bytes": table_path.stat().st_size,
                    "sha256": _sha256_file(table_path),
                }
            },
            "lookup_counts": {
                "distinct_ips": row_count,
                "country_lookups": row_count,
                "country_found": country_found,
                "asn_lookups": row_count,
                "asn_found": asn_found,
            },
        }
        (staging / MANIFEST_FILE_NAME).write_bytes(_pretty_json(manifest))

        from bitcoin_intel.enrichment.validation import validate_enrichment_store

        report = validate_enrichment_store(staging, dataset.path)
        if not report.is_valid:
            details = "; ".join(f"{issue.code}={issue.count}" for issue in report.issues)
            raise EnrichmentBuildError(f"staged enrichment store failed validation: {details}")
        if destination.exists():
            raise EnrichmentBuildError(
                "enrichment output was created concurrently and will not be overwritten: "
                f"{destination}"
            )
        staging.replace(destination)
        return EnrichmentBuildSummary(
            destination, enrichment_dataset_id, row_count, country_found, asn_found
        )
    except Exception:
        _LOGGER.exception("enrichment build failed: dataset=%s", dataset.path)
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _write_rows(
    dataset: AnalyticalDataset, resources: OfflineGeoIPReaders, output_path: Path
) -> tuple[int, int, int]:
    rows = country_found = asn_found = 0
    with (
        dataset.connect() as connection,
        pq.ParquetWriter(
            output_path,
            IP_ENRICHMENT_SCHEMA,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
            version="2.6",
        ) as writer,
    ):
        reader = connection.execute(
            """SELECT ip FROM (
            SELECT src_ip AS ip FROM network_observations
            UNION SELECT dst_ip AS ip FROM network_observations
            ) ORDER BY ip"""
        ).to_arrow_reader(_ROW_GROUP_SIZE)
        for batch in reader:
            enriched = [_enrich_ip(str(ip), resources) for ip in batch.column(0).to_pylist()]
            table = pa.Table.from_pylist(enriched, schema=IP_ENRICHMENT_SCHEMA)
            writer.write_table(table, row_group_size=_ROW_GROUP_SIZE)
            rows += table.num_rows
            country_found += sum(bool(value) for value in table["country_found"].to_pylist())
            asn_found += sum(bool(value) for value in table["asn_found"].to_pylist())
    return rows, country_found, asn_found


def _enrich_ip(ip_text: str, resources: OfflineGeoIPReaders) -> dict[str, object]:
    address = ipaddress.ip_address(ip_text)
    country_record, country_prefix = _lookup(resources.country, ip_text)
    asn_record, asn_prefix = _lookup(resources.asn, ip_text)
    country_code = _country_code(country_record)
    asn, as_org = _asn_values(asn_record)
    return {
        "ip": address.compressed,
        "enriched_country_code": country_code,
        "enriched_asn": asn,
        "enriched_as_org": as_org,
        "country_found": country_code is not None,
        "asn_found": asn is not None or as_org is not None,
        "country_network": _network(address, country_prefix) if country_code else None,
        "asn_network": _network(address, asn_prefix) if asn is not None or as_org else None,
        "is_private": address.is_private,
        "is_global": address.is_global,
        "is_loopback": address.is_loopback,
        "is_link_local": address.is_link_local,
        "is_multicast": address.is_multicast,
        "is_reserved": address.is_reserved,
    }


def _lookup(reader: Any, ip: str) -> tuple[dict[str, Any] | None, int | None]:
    try:
        record, prefix = reader.get_with_prefix_len(ip)
    except ValueError:
        return None, None
    return (record if isinstance(record, dict) else None), (int(prefix) if record else None)


def _country_code(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    country = record.get("country")
    value = country.get("iso_code") if isinstance(country, dict) else None
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if len(normalized) == 2 and normalized.isalpha() else None


def _asn_values(record: dict[str, Any] | None) -> tuple[int | None, str | None]:
    if not record:
        return None, None
    traits = record.get("traits")
    source = traits if isinstance(traits, dict) else record
    raw_asn = source.get("autonomous_system_number")
    raw_org = source.get("autonomous_system_organization")
    asn = (
        raw_asn
        if isinstance(raw_asn, int) and not isinstance(raw_asn, bool) and raw_asn > 0
        else None
    )
    org = raw_org.strip() if isinstance(raw_org, str) and raw_org.strip() else None
    return asn, org


def _network(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, prefix: int | None
) -> str | None:
    if prefix is None:
        return None
    if address.version == 4 and prefix > 32:
        prefix -= 96
    if prefix < 0 or prefix > address.max_prefixlen:
        return None
    return ipaddress.ip_network(f"{address}/{prefix}", strict=False).with_prefixlen


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
