from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa

FEATURE_SCHEMA_VERSION = "1.0.0"
FEATURE_CALCULATION_VERSION = "1"
PART_FILE_NAME = "part-00000.parquet"
MANIFEST_FILE_NAME = "feature-manifest.json"
DEFINITIONS_FILE_NAME = "feature-definitions.json"


@dataclass(frozen=True, slots=True)
class FeatureTableDefinition:
    key: str
    schema: pa.Schema


@dataclass(frozen=True, slots=True)
class FeatureBuildConfig:
    cutoff: datetime | None = None
    reused_ip_min_transactions: int = 2

    def __post_init__(self) -> None:
        if self.cutoff is not None and (
            self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None
        ):
            raise ValueError("feature cutoff must include an explicit timezone")
        if self.reused_ip_min_transactions < 2:
            raise ValueError("reused IP threshold must be at least 2 distinct transactions")


@dataclass(frozen=True, slots=True)
class FeatureBuildSummary:
    output_path: Path
    feature_dataset_id: str
    temporal_mode: str
    cutoff: datetime | None
    table_rows: dict[str, int]


@dataclass(frozen=True, slots=True)
class FeatureValidationIssue:
    code: str
    count: int
    message: str


@dataclass(frozen=True, slots=True)
class FeatureValidationReport:
    issues: tuple[FeatureValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


_S = pa.string()
_I = pa.int64()
_F = pa.float64()
_T = pa.timestamp("us", tz="UTC")


def _field(name: str, dtype: pa.DataType, nullable: bool = False) -> pa.Field:
    return pa.field(name, dtype, nullable=nullable)


TRANSACTION_FEATURES = FeatureTableDefinition(
    key="txid",
    schema=pa.schema(
        [
            _field("txid", _S),
            _field("input_count", _I),
            _field("output_count", _I),
            _field("total_input_sats", _I),
            _field("total_output_sats", _I),
            _field("fee_sats", _I),
            _field("mean_input_sats", _F, True),
            _field("mean_output_sats", _F, True),
            _field("max_input_sats", _I, True),
            _field("max_output_sats", _I, True),
            _field("min_input_sats", _I, True),
            _field("min_output_sats", _I, True),
            _field("input_value_std", _F, True),
            _field("output_value_std", _F, True),
            _field("fee_to_input_ratio", _F, True),
            _field("network_observation_count", _I),
            _field("unique_source_ip_count", _I),
            _field("unique_destination_ip_count", _I),
            _field("unique_ip_count", _I),
            _field("unique_reported_asn_count", _I),
            _field("unique_reported_country_count", _I),
            _field("first_observed_at", _T, True),
            _field("last_observed_at", _T, True),
            _field("observation_span_seconds", _I, True),
            _field("mean_inter_observation_seconds", _F, True),
            _field("median_inter_observation_seconds", _F, True),
            _field("min_inter_observation_seconds", _I, True),
            _field("max_inter_observation_seconds", _I, True),
            _field("active_hour_count", _I),
            _field("observations_per_active_hour", _F, True),
            _field("hour_of_day_entropy", _F, True),
            _field("day_activity_entropy", _F, True),
            _field("max_observations_1m", _I),
            _field("max_observations_5m", _I),
            _field("max_observations_1h", _I),
        ]
    ),
)

ADDRESS_FEATURES = FeatureTableDefinition(
    key="address",
    schema=pa.schema(
        [
            _field("address", _S),
            _field("input_occurrence_count", _I),
            _field("output_occurrence_count", _I),
            _field("unique_input_tx_count", _I),
            _field("unique_output_tx_count", _I),
            _field("unique_tx_count", _I),
            _field("total_input_sats", _I),
            _field("total_output_sats", _I),
            _field("mean_input_sats", _F, True),
            _field("mean_output_sats", _F, True),
            _field("max_input_sats", _I, True),
            _field("max_output_sats", _I, True),
            _field("input_output_tx_ratio", _F, True),
            _field("input_output_value_ratio", _F, True),
            _field("co_transaction_address_count", _I),
            _field("network_observation_count", _I),
            _field("first_observed_at", _T, True),
            _field("last_observed_at", _T, True),
            _field("observation_span_seconds", _I, True),
            _field("mean_inter_observation_seconds", _F, True),
            _field("median_inter_observation_seconds", _F, True),
            _field("min_inter_observation_seconds", _I, True),
            _field("max_inter_observation_seconds", _I, True),
            _field("active_hour_count", _I),
            _field("observations_per_active_hour", _F, True),
            _field("bipartite_component_size", _I),
        ]
    ),
)

IP_FEATURES = FeatureTableDefinition(
    key="ip",
    schema=pa.schema(
        [
            _field("ip", _S),
            _field("source_observation_count", _I),
            _field("destination_observation_count", _I),
            _field("total_observation_count", _I),
            _field("unique_tx_count", _I),
            _field("unique_src_port_count", _I),
            _field("unique_dst_port_count", _I),
            _field("unique_port_count", _I),
            _field("unique_reported_asn_count", _I),
            _field("unique_reported_country_count", _I),
            _field("first_observed_at", _T),
            _field("last_observed_at", _T),
            _field("observation_span_seconds", _I),
            _field("mean_inter_observation_seconds", _F, True),
            _field("median_inter_observation_seconds", _F, True),
            _field("min_inter_observation_seconds", _I, True),
            _field("max_inter_observation_seconds", _I, True),
            _field("active_hour_count", _I),
            _field("observations_per_active_hour", _F),
            _field("hour_of_day_entropy", _F),
            _field("day_activity_entropy", _F),
            _field("max_observations_1m", _I),
            _field("max_observations_5m", _I),
            _field("max_observations_1h", _I),
        ]
    ),
)

CORRELATION_FEATURES = FeatureTableDefinition(
    key="address",
    schema=pa.schema(
        [
            _field("address", _S),
            _field("network_observation_count", _I),
            _field("distinct_associated_ip_count", _I),
            _field("distinct_source_ip_count", _I),
            _field("distinct_destination_ip_count", _I),
            _field("unique_reported_asn_count", _I),
            _field("unique_reported_country_count", _I),
            _field("reused_ip_count", _I),
            _field("max_transactions_per_associated_ip", _I, True),
            _field("mean_transactions_per_associated_ip", _F, True),
            _field("ip_reuse_ratio", _F, True),
        ]
    ),
)

FEATURE_TABLES: dict[str, FeatureTableDefinition] = {
    "transaction_features": TRANSACTION_FEATURES,
    "address_features": ADDRESS_FEATURES,
    "ip_features": IP_FEATURES,
    "correlation_features": CORRELATION_FEATURES,
}
