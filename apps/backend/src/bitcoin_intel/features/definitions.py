# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
from typing import Any

import pyarrow as pa

from bitcoin_intel.features.models import (
    FEATURE_CALCULATION_VERSION,
    FEATURE_SCHEMA_VERSION,
    feature_tables_for_version,
)

# Registry descriptions are intentionally complete scalar metadata; splitting every value would
# make this machine-oriented mapping harder to audit against emitted JSON.

_TABLE_METADATA: dict[str, dict[str, Any]] = {
    "transaction_features": {
        "entity_type": "transaction",
        "source_tables": [
            "transactions",
            "transaction_inputs",
            "transaction_outputs",
            "network_observations",
        ],
        "description": "Canonical transaction value structure and scoped network-observation behaviour.",
    },
    "address_features": {
        "entity_type": "address",
        "source_tables": ["transaction_inputs", "transaction_outputs", "network_observations"],
        "description": "Address occurrence, co-transaction, temporal, and bipartite topology measurements.",
    },
    "ip_features": {
        "entity_type": "ip",
        "source_tables": ["network_observations"],
        "description": "Source/destination endpoint and temporal observation measurements.",
    },
    "correlation_features": {
        "entity_type": "address",
        "source_tables": ["transaction_inputs", "transaction_outputs", "network_observations"],
        "description": "Observational address-to-network associations through containing transactions.",
    },
}

_DESCRIPTIONS = {
    "txid": "Canonical transaction identifier.",
    "address": "Canonical address string; no wallet or owner semantics are implied.",
    "ip": "Canonical normalized IP endpoint string.",
    "input_count": "Number of canonical input rows for the transaction.",
    "output_count": "Number of canonical output rows for the transaction.",
    "input_occurrence_count": "Number of canonical input rows containing the address.",
    "output_occurrence_count": "Number of canonical output rows containing the address.",
    "unique_input_tx_count": "Distinct scoped transactions where the address appears as an input.",
    "unique_output_tx_count": "Distinct scoped transactions where the address appears as an output.",
    "unique_tx_count": "Distinct scoped transactions associated with the entity.",
    "total_input_sats": "Exact integer sum of canonical input satoshis.",
    "total_output_sats": "Exact integer sum of canonical output satoshis.",
    "fee_sats": "Canonical integer transaction fee in satoshis.",
    "mean_input_sats": "Arithmetic mean input value in satoshis; NULL without inputs.",
    "mean_output_sats": "Arithmetic mean output value in satoshis; NULL without outputs.",
    "max_input_sats": "Maximum canonical input value; NULL without inputs.",
    "max_output_sats": "Maximum canonical output value; NULL without outputs.",
    "min_input_sats": "Minimum canonical input value; NULL without inputs.",
    "min_output_sats": "Minimum canonical output value; NULL without outputs.",
    "input_value_std": "Sample standard deviation of input values; NULL with fewer than two inputs.",
    "output_value_std": "Sample standard deviation of output values; NULL with fewer than two outputs.",
    "fee_to_input_ratio": "Fee divided by total input value; NULL when total input is zero.",
    "input_output_tx_ratio": "Distinct input transaction count divided by distinct output transaction count; NULL on zero denominator.",
    "input_output_value_ratio": "Total input satoshis divided by total output satoshis; NULL on zero denominator.",
    "co_transaction_address_count": "Distinct other addresses sharing at least one scoped transaction; self is excluded and direct payment is not implied.",
    "network_observation_count": "Distinct scoped network observations associated through the entity's transactions.",
    "source_observation_count": "Distinct observations where the IP has source role.",
    "destination_observation_count": "Distinct observations where the IP has destination role.",
    "total_observation_count": "Distinct observations containing the IP in either endpoint role.",
    "unique_source_ip_count": "Distinct source-role IPs on scoped observations.",
    "unique_destination_ip_count": "Distinct destination-role IPs on scoped observations.",
    "unique_ip_count": "Distinct IPs in either endpoint role on scoped observations.",
    "distinct_associated_ip_count": "Distinct IPs observed on transactions containing the address; ownership is not implied.",
    "distinct_source_ip_count": "Distinct source-role IPs observed through transactions containing the address.",
    "distinct_destination_ip_count": "Distinct destination-role IPs observed through transactions containing the address.",
    "unique_src_port_count": "Distinct source ports observed for the IP in source role.",
    "unique_dst_port_count": "Distinct destination ports observed for the IP in destination role.",
    "unique_port_count": "Distinct role-appropriate ports observed for the IP.",
    "unique_reported_asn_count": "Distinct non-NULL source-reported ASN values in associated observations.",
    "unique_reported_country_count": "Distinct non-NULL source-reported country values in associated observations.",
    "first_observed_at": "Earliest network observation timestamp in UTC; not block or confirmation time.",
    "last_observed_at": "Latest network observation timestamp in UTC; not block or confirmation time.",
    "observation_span_seconds": "Whole seconds between first and last observation; NULL without observations.",
    "mean_inter_observation_seconds": "Mean elapsed seconds between deterministically ordered consecutive observations; NULL with fewer than two.",
    "median_inter_observation_seconds": "Median elapsed seconds between deterministically ordered consecutive observations; NULL with fewer than two.",
    "min_inter_observation_seconds": "Minimum elapsed whole seconds between consecutive observations; NULL with fewer than two.",
    "max_inter_observation_seconds": "Maximum elapsed whole seconds between consecutive observations; NULL with fewer than two.",
    "active_hour_count": "Distinct UTC clock-hour buckets containing observations.",
    "observations_per_active_hour": "Observation count divided by active UTC hour buckets; NULL with no active hour.",
    "hour_of_day_entropy": "Natural-log Shannon entropy of observations across UTC hours of day.",
    "day_activity_entropy": "Natural-log Shannon entropy of observations across UTC calendar days.",
    "max_observations_1m": "Maximum observations in an inclusive trailing one-minute window.",
    "max_observations_5m": "Maximum observations in an inclusive trailing five-minute window.",
    "max_observations_1h": "Maximum observations in an inclusive trailing one-hour window.",
    "bipartite_component_size": "Address plus transaction vertex count in the scoped factual bipartite WCC; same component does not imply same entity.",
    "reused_ip_count": "Associated IPs linked through at least the configured number of distinct containing transactions.",
    "max_transactions_per_associated_ip": "Maximum distinct containing transactions for one associated IP; NULL without associated IPs.",
    "mean_transactions_per_associated_ip": "Mean distinct containing transactions per associated IP; NULL without associated IPs.",
    "ip_reuse_ratio": "Reused associated IP count divided by distinct associated IP count; NULL without associated IPs.",
    "unique_enriched_country_count": "Distinct endpoint-enriched countries on the transaction's scoped observations.",
    "unique_enriched_asn_count": "Distinct endpoint-enriched ASNs on the transaction's scoped observations.",
    "source_destination_country_match_rate": "Fraction of scoped observations with two known endpoint countries where source and destination match; NULL without comparable pairs.",
    "source_destination_asn_match_rate": "Fraction of scoped observations with two known endpoint ASNs where source and destination match; NULL without comparable pairs.",
    "associated_enriched_country_count": "Distinct endpoint-enriched countries associated through scoped transactions containing the address.",
    "associated_enriched_asn_count": "Distinct endpoint-enriched ASNs associated through scoped transactions containing the address.",
    "associated_cross_country_observation_count": "Distinct associated observations whose two known endpoint countries differ.",
    "associated_cross_asn_observation_count": "Distinct associated observations whose two known endpoint ASNs differ.",
}

_NETWORK_PREFIXES = (
    "network_",
    "unique_source_ip",
    "unique_destination_ip",
    "unique_ip_",
    "distinct_associated_ip",
    "distinct_source_ip",
    "distinct_destination_ip",
    "source_observation",
    "destination_observation",
    "total_observation",
    "unique_src_port",
    "unique_dst_port",
    "unique_port",
    "unique_reported_",
    "unique_enriched_",
    "source_destination_",
    "associated_enriched_",
    "associated_cross_",
    "first_observed",
    "last_observed",
    "observation_span",
    "mean_inter_",
    "median_inter_",
    "min_inter_",
    "max_inter_",
    "active_hour",
    "observations_per_",
    "hour_of_day_",
    "day_activity_",
    "max_observations_",
    "reused_ip",
    "max_transactions_per_associated_ip",
    "mean_transactions_per_associated_ip",
    "ip_reuse_ratio",
)


def build_definition_registry(schema_version: str = FEATURE_SCHEMA_VERSION) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    table_metadata = json.loads(json.dumps(_TABLE_METADATA))
    if schema_version == FEATURE_SCHEMA_VERSION:
        for name in ("transaction_features", "correlation_features"):
            table_metadata[name]["source_tables"].append("ip_enrichment")
    for table_name, table in feature_tables_for_version(schema_version).items():
        metadata = table_metadata[table_name]
        for field in table.schema:
            temporal_semantics = _temporal_semantics(field.name)
            features.append(
                {
                    "name": field.name,
                    "table": table_name,
                    "entity_type": metadata["entity_type"],
                    "dtype": _dtype_name(field.type),
                    "description": _DESCRIPTIONS[field.name],
                    "source_tables": metadata["source_tables"],
                    "calculation_version": FEATURE_CALCULATION_VERSION,
                    "temporal_semantics": temporal_semantics,
                    "nullable": field.nullable,
                    "unit": _unit(field.name),
                }
            )
    return {
        "feature_schema_version": schema_version,
        "calculation_version": FEATURE_CALCULATION_VERSION,
        "tables": table_metadata,
        "features": features,
    }


def serialize_definition_registry(schema_version: str = FEATURE_SCHEMA_VERSION) -> bytes:
    return (
        json.dumps(build_definition_registry(schema_version), indent=2, sort_keys=True) + "\n"
    ).encode()


def definition_registry_sha256(schema_version: str = FEATURE_SCHEMA_VERSION) -> str:
    return hashlib.sha256(serialize_definition_registry(schema_version)).hexdigest()


def _temporal_semantics(name: str) -> str:
    if name == "bipartite_component_size":
        return "build-scope; cutoff-safe when the projection is cutoff-filtered"
    if name.startswith(_NETWORK_PREFIXES):
        return "network-observation cutoff-safe"
    if name in {"txid", "address", "ip"}:
        return "canonical identity within build scope"
    return "canonical transaction fact admitted by build scope"


def _dtype_name(dtype: pa.DataType) -> str:
    if pa.types.is_string(dtype):
        return "string"
    if pa.types.is_int64(dtype):
        return "int64"
    if pa.types.is_float64(dtype):
        return "float64"
    if pa.types.is_timestamp(dtype):
        return "timestamp[us, UTC]"
    raise AssertionError(f"unsupported feature dtype: {dtype}")


def _unit(name: str) -> str | None:
    if name.endswith("_sats"):
        return "satoshi"
    if name.endswith("_seconds"):
        return "second"
    if name.endswith("_at"):
        return "UTC timestamp"
    if name.endswith("_entropy"):
        return "nat"
    if name.endswith("_ratio") or name == "observations_per_active_hour":
        return "ratio"
    if name.endswith("_count") or name.startswith("max_observations_") or name.endswith("_size"):
        return "count"
    return None
