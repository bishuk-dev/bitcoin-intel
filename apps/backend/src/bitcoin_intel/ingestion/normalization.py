from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bitcoin_intel.ingestion.adapters import SourceFormat
from bitcoin_intel.ingestion.errors import ErrorCode, RecordIssue
from bitcoin_intel.ingestion.models import ValidatedInputRecord
from bitcoin_intel.ingestion.validation import build_observation_id


@dataclass(frozen=True, slots=True)
class TransactionRow:
    txid: str
    fee_sats: int
    script_type: str | None


@dataclass(frozen=True, slots=True)
class TransactionInputRow:
    txid: str
    input_index: int
    address: str
    amount_sats: int


@dataclass(frozen=True, slots=True)
class TransactionOutputRow:
    txid: str
    output_index: int
    address: str
    amount_sats: int


@dataclass(frozen=True, slots=True)
class NetworkObservationRow:
    observation_id: str
    txid: str
    observed_at: datetime
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    reported_geo_country: str | None
    reported_asn: int | None
    source_record_id: str


@dataclass(frozen=True, slots=True)
class TransactionSourceRow:
    txid: str
    source_record_id: str


@dataclass(frozen=True, slots=True)
class SourceRecordRow:
    source_record_id: str
    source_file: str
    source_format: str
    source_file_sha256: str
    record_index: int


@dataclass(frozen=True, slots=True)
class RejectedRecordRow:
    source_record_id: str
    source_file: str
    record_index: int
    error_code: str
    error_message: str
    field_name: str | None


@dataclass(frozen=True, slots=True)
class CanonicalDataset:
    transactions: list[TransactionRow]
    transaction_inputs: list[TransactionInputRow]
    transaction_outputs: list[TransactionOutputRow]
    network_observations: list[NetworkObservationRow]
    transaction_sources: list[TransactionSourceRow]
    source_records: list[SourceRecordRow]
    rejected_records: list[RejectedRecordRow]


@dataclass(frozen=True, slots=True)
class _TransactionDefinition:
    fee_sats: int
    script_type: str | None
    inputs: tuple[tuple[str, int], ...]
    outputs: tuple[tuple[str, int], ...]

    @classmethod
    def from_record(cls, record: ValidatedInputRecord) -> _TransactionDefinition:
        return cls(
            fee_sats=record.fee_sats,
            script_type=record.script_type,
            inputs=tuple(
                (address, record.input_amounts_sats[index])
                for index, address in enumerate(record.input_addresses)
            ),
            outputs=tuple(
                (address, record.output_amounts_sats[index])
                for index, address in enumerate(record.output_addresses)
            ),
        )


class CanonicalAccumulator:
    """Apply deterministic transaction deduplication and build canonical table rows."""

    def __init__(self) -> None:
        self._definitions: dict[str, _TransactionDefinition] = {}
        self._accepted_source_ids: set[str] = set()
        self.transactions: list[TransactionRow] = []
        self.transaction_inputs: list[TransactionInputRow] = []
        self.transaction_outputs: list[TransactionOutputRow] = []
        self.network_observations: list[NetworkObservationRow] = []
        self.transaction_sources: list[TransactionSourceRow] = []
        self.source_records: list[SourceRecordRow] = []
        self.rejected_records: list[RejectedRecordRow] = []

    def add_source_record(
        self,
        *,
        source_record_id: str,
        source_file: str,
        source_format: SourceFormat,
        source_file_sha256: str,
        record_index: int,
    ) -> None:
        self.source_records.append(
            SourceRecordRow(
                source_record_id=source_record_id,
                source_file=source_file,
                source_format=source_format.value,
                source_file_sha256=source_file_sha256,
                record_index=record_index,
            )
        )

    def accept(self, record: ValidatedInputRecord, *, source_record_id: str) -> RecordIssue | None:
        definition = _TransactionDefinition.from_record(record)
        existing = self._definitions.get(record.txid)
        if existing is not None and existing != definition:
            return RecordIssue(
                ErrorCode.TXID_CONTENT_CONFLICT,
                f"txid {record.txid} conflicts with an earlier accepted blockchain definition",
                "txid",
            )

        if existing is None:
            self._definitions[record.txid] = definition
            self.transactions.append(
                TransactionRow(record.txid, record.fee_sats, record.script_type)
            )
            self.transaction_inputs.extend(
                TransactionInputRow(record.txid, index, address, amount_sats)
                for index, (address, amount_sats) in enumerate(definition.inputs)
            )
            self.transaction_outputs.extend(
                TransactionOutputRow(record.txid, index, address, amount_sats)
                for index, (address, amount_sats) in enumerate(definition.outputs)
            )

        if source_record_id not in self._accepted_source_ids:
            self._accepted_source_ids.add(source_record_id)
            self.transaction_sources.append(TransactionSourceRow(record.txid, source_record_id))
            self.network_observations.append(
                NetworkObservationRow(
                    observation_id=build_observation_id(source_record_id),
                    txid=record.txid,
                    observed_at=record.observed_at,
                    src_ip=record.src_ip,
                    dst_ip=record.dst_ip,
                    src_port=record.src_port,
                    dst_port=record.dst_port,
                    reported_geo_country=record.reported_geo_country,
                    reported_asn=record.reported_asn,
                    source_record_id=source_record_id,
                )
            )
        return None

    def reject(
        self,
        *,
        source_record_id: str,
        source_file: str,
        record_index: int,
        issue: RecordIssue,
    ) -> None:
        self.rejected_records.append(
            RejectedRecordRow(
                source_record_id=source_record_id,
                source_file=source_file,
                record_index=record_index,
                error_code=issue.code.value,
                error_message=issue.message[:512],
                field_name=issue.field_name,
            )
        )

    def build(self) -> CanonicalDataset:
        return CanonicalDataset(
            transactions=self.transactions,
            transaction_inputs=self.transaction_inputs,
            transaction_outputs=self.transaction_outputs,
            network_observations=self.network_observations,
            transaction_sources=self.transaction_sources,
            source_records=self.source_records,
            rejected_records=self.rejected_records,
        )
