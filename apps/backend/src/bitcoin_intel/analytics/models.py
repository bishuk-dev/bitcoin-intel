from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Transaction:
    txid: str
    fee_sats: int
    script_type: str | None


@dataclass(frozen=True, slots=True)
class TransactionInput:
    input_index: int
    address: str
    amount_sats: int


@dataclass(frozen=True, slots=True)
class TransactionOutput:
    output_index: int
    address: str
    amount_sats: int


@dataclass(frozen=True, slots=True)
class NetworkObservation:
    observation_id: str
    observed_at: datetime
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    reported_geo_country: str | None
    reported_asn: int | None
    source_record_id: str


@dataclass(frozen=True, slots=True)
class TransactionProvenance:
    source_record_id: str
    source_file: str
    source_format: str
    source_file_sha256: str
    record_index: int


@dataclass(frozen=True, slots=True)
class TransactionDetail:
    transaction: Transaction
    inputs: tuple[TransactionInput, ...]
    outputs: tuple[TransactionOutput, ...]
    observations: tuple[NetworkObservation, ...]
    provenance: tuple[TransactionProvenance, ...]


@dataclass(frozen=True, slots=True)
class TransactionSummary:
    txid: str
    input_count: int
    output_count: int
    total_input_sats: int
    total_output_sats: int
    fee_sats: int
    network_observation_count: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class HighFeeTransaction:
    summary: TransactionSummary
    fee_to_input_ratio: float | None


@dataclass(frozen=True, slots=True)
class AddressActivitySummary:
    address: str
    transaction_count: int
    input_count: int
    output_count: int
    total_input_sats: int
    total_output_sats: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class IpActivitySummary:
    ip: str
    observation_count: int
    unique_txids: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    source_role_count: int
    destination_role_count: int
    unique_ports: tuple[int, ...]
    reported_asns: tuple[int, ...]
    reported_countries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AsnActivitySummary:
    reported_asn: int
    observation_count: int
    unique_ips: int
    unique_txids: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TemporalActivity:
    bucket_start: datetime
    observation_count: int
    unique_txids: int
    unique_source_ips: int
    unique_destination_ips: int


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    code: str
    count: int
    message: str


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    issues: tuple[IntegrityIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues
