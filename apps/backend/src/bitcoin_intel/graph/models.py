from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class GraphImportFile:
    path: Path
    header_path: Path
    rows: int
    bytes: int
    sha256: str
    header_sha256: str


@dataclass(frozen=True, slots=True)
class GraphImportManifest:
    graph_schema_version: str
    canonical_schema_version: str
    canonical_manifest_sha256: str
    neo4j_version: str
    built_at: datetime
    node_counts: dict[str, int]
    relationship_counts: dict[str, int]
    files: dict[str, GraphImportFile]


@dataclass(frozen=True, slots=True)
class PreparedGraphImport:
    path: Path
    manifest: GraphImportManifest


@dataclass(frozen=True, slots=True)
class GraphImportIssue:
    code: str
    count: int
    message: str


@dataclass(frozen=True, slots=True)
class GraphImportValidationReport:
    issues: tuple[GraphImportIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class GraphCounts:
    transactions: int
    addresses: int
    ip_addresses: int
    network_observations: int
    spent_in: int
    created_output: int
    observed_transaction: int
    source_ip: int
    destination_ip: int


@dataclass(frozen=True, slots=True)
class GraphIntegrityIssue:
    code: str
    count: int
    message: str


@dataclass(frozen=True, slots=True)
class GraphIntegrityReport:
    graph_counts: GraphCounts
    canonical_counts: GraphCounts
    issues: tuple[GraphIntegrityIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class GraphTransaction:
    txid: str
    fee_sats: int
    script_type: str | None


@dataclass(frozen=True, slots=True)
class GraphAddressUse:
    address: str
    index: int
    amount_sats: int


@dataclass(frozen=True, slots=True)
class GraphObservation:
    observation_id: str
    observed_at: datetime
    src_port: int
    dst_port: int
    reported_geo_country: str | None
    reported_asn: int | None
    source_record_id: str
    source_ip: str
    destination_ip: str
    txid: str


@dataclass(frozen=True, slots=True)
class TransactionNeighborhood:
    transaction: GraphTransaction
    inputs: tuple[GraphAddressUse, ...]
    outputs: tuple[GraphAddressUse, ...]
    observations: tuple[GraphObservation, ...]


@dataclass(frozen=True, slots=True)
class AddressTransactionUse:
    txid: str
    role: str
    index: int
    amount_sats: int


@dataclass(frozen=True, slots=True)
class AddressTransactions:
    address: str
    uses: tuple[AddressTransactionUse, ...]


@dataclass(frozen=True, slots=True)
class IpObservationUse:
    role: str
    observation: GraphObservation


@dataclass(frozen=True, slots=True)
class IpObservations:
    ip: str
    uses: tuple[IpObservationUse, ...]


@dataclass(frozen=True, slots=True)
class GraphNodeIdentity:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class GraphPath:
    nodes: tuple[GraphNodeIdentity, ...]
    relationship_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginVersions:
    neo4j: str
    edition: str
    gds: str
    apoc: str


@dataclass(frozen=True, slots=True)
class GdsVerification:
    graph_name: str
    estimated_bytes_min: int
    estimated_bytes_max: int
    node_count: int
    relationship_count: int
    component_count: int
    project_millis: int
    compute_millis: int


RecordData = dict[str, Any]
