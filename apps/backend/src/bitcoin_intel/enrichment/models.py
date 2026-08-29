from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

ENRICHMENT_SCHEMA_VERSION = "1.0.0"
MANIFEST_FILE_NAME = "enrichment-manifest.json"
TABLE_NAME = "ip_enrichment"
PART_FILE_NAME = "part-00000.parquet"


@dataclass(frozen=True, slots=True)
class EnrichmentBuildSummary:
    output_path: Path
    enrichment_dataset_id: str
    canonical_ip_count: int
    country_found_count: int
    asn_found_count: int


@dataclass(frozen=True, slots=True)
class EnrichmentValidationIssue:
    code: str
    count: int
    message: str


@dataclass(frozen=True, slots=True)
class EnrichmentValidationReport:
    issues: tuple[EnrichmentValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


IP_ENRICHMENT_SCHEMA = pa.schema(
    [
        pa.field("ip", pa.string(), nullable=False),
        pa.field("enriched_country_code", pa.string()),
        pa.field("enriched_asn", pa.int64()),
        pa.field("enriched_as_org", pa.string()),
        pa.field("country_found", pa.bool_(), nullable=False),
        pa.field("asn_found", pa.bool_(), nullable=False),
        pa.field("country_network", pa.string()),
        pa.field("asn_network", pa.string()),
        pa.field("is_private", pa.bool_(), nullable=False),
        pa.field("is_global", pa.bool_(), nullable=False),
        pa.field("is_loopback", pa.bool_(), nullable=False),
        pa.field("is_link_local", pa.bool_(), nullable=False),
        pa.field("is_multicast", pa.bool_(), nullable=False),
        pa.field("is_reserved", pa.bool_(), nullable=False),
    ]
)
