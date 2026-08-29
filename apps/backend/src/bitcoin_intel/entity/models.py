from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pyarrow as pa

ENTITY_SCHEMA_VERSION = "1.0.0"
ENTITY_METHOD_VERSION = "conservative-mih-v1"
MANIFEST_FILE_NAME = "entity-manifest.json"
PART_FILE_NAME = "part-00000.parquet"


@dataclass(frozen=True, slots=True)
class EntityBuildConfig:
    collaborative_min_inputs: int = 3
    collaborative_min_outputs: int = 3
    collaborative_min_equal_outputs: int = 2
    collaborative_min_equal_fraction: float = 0.5
    behavioral_min_cluster_size: int = 5
    behavioral_min_samples: int = 3
    leiden_seed: int = 42

    def __post_init__(self) -> None:
        for name in (
            "collaborative_min_inputs",
            "collaborative_min_outputs",
            "collaborative_min_equal_outputs",
            "behavioral_min_cluster_size",
            "behavioral_min_samples",
        ):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be at least 2")
        if not 0.0 < self.collaborative_min_equal_fraction <= 1.0:
            raise ValueError("collaborative_min_equal_fraction must be in (0, 1]")
        if self.leiden_seed < 0:
            raise ValueError("leiden_seed must be non-negative")

    def semantic_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EntityBuildSummary:
    output_path: Path
    entity_dataset_id: str
    candidate_entity_count: int
    address_count: int
    suppressed_transaction_count: int
    behavioral_noise_count: int
    table_rows: dict[str, int]


@dataclass(frozen=True, slots=True)
class EntityValidationIssue:
    code: str
    count: int
    message: str


@dataclass(frozen=True, slots=True)
class EntityValidationReport:
    issues: tuple[EntityValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class EntityTableDefinition:
    key: tuple[str, ...]
    schema: pa.Schema
    sort_by: tuple[str, ...]


_S = pa.string()
_I = pa.int64()
_F = pa.float64()
_B = pa.bool_()


def _field(name: str, dtype: pa.DataType, nullable: bool = False) -> pa.Field:
    return pa.field(name, dtype, nullable=nullable)


ENTITY_TABLES: dict[str, EntityTableDefinition] = {
    "candidate_entities": EntityTableDefinition(
        ("candidate_id",),
        pa.schema(
            [
                _field("candidate_id", _S),
                _field("member_count", _I),
                _field("strong_evidence_edge_count", _I),
                _field("supporting_evidence_count", _I),
                _field("merge_transaction_count", _I),
                _field("fragile_bridge_count", _I),
                _field("edge_density", _F),
                _field("minimum_degree", _I),
                _field("transitive_only_member_count", _I),
                _field("robustness_score", _F),
                _field("method_version", _S),
            ]
        ),
        ("candidate_id",),
    ),
    "candidate_memberships": EntityTableDefinition(
        ("address",),
        pa.schema(
            [
                _field("candidate_id", _S),
                _field("address", _S),
                _field("membership_support", _F),
                _field("direct_to_anchor", _B),
                _field("transitive_only", _B),
                _field("accepted_evidence_count", _I),
            ]
        ),
        ("address",),
    ),
    "ownership_evidence": EntityTableDefinition(
        ("evidence_id",),
        pa.schema(
            [
                _field("evidence_id", _S),
                _field("address_a", _S),
                _field("address_b", _S),
                _field("evidence_type", _S),
                _field("evidence_source_id", _S),
                _field("strength_class", _S),
                _field("support_value", _F),
                _field("merge_selected", _B),
                _field("suppressed", _B),
                _field("suppression_reason", _S, True),
                _field("fragile_bridge", _B),
            ]
        ),
        ("evidence_id",),
    ),
    "collaborative_transactions": EntityTableDefinition(
        ("txid",),
        pa.schema(
            [
                _field("txid", _S),
                _field("distinct_input_count", _I),
                _field("distinct_output_count", _I),
                _field("equal_output_group_count", _I),
                _field("max_equal_output_multiplicity", _I),
                _field("equal_output_fraction", _F),
                _field("balanced_output_fraction", _F),
                _field("collaborative_tx_suspected", _B),
                _field("suppression_reason", _S, True),
            ]
        ),
        ("txid",),
    ),
    "behavioral_communities": EntityTableDefinition(
        ("address",),
        pa.schema(
            [
                _field("address", _S),
                _field("behavioral_community_id", _S, True),
                _field("community_size", _I),
                _field("is_noise", _B),
                _field("algorithm", _S),
            ]
        ),
        ("address",),
    ),
    "topological_communities": EntityTableDefinition(
        ("address",),
        pa.schema(
            [
                _field("address", _S),
                _field("topological_community_id", _S),
                _field("community_size", _I),
                _field("algorithm", _S),
            ]
        ),
        ("address",),
    ),
}


BEHAVIORAL_FEATURE_COLUMNS = (
    "input_occurrence_count",
    "output_occurrence_count",
    "unique_input_tx_count",
    "unique_output_tx_count",
    "unique_tx_count",
    "total_input_sats",
    "total_output_sats",
    "mean_input_sats",
    "mean_output_sats",
    "input_output_tx_ratio",
    "input_output_value_ratio",
    "co_transaction_address_count",
    "network_observation_count",
    "observation_span_seconds",
    "mean_inter_observation_seconds",
    "active_hour_count",
    "observations_per_active_hour",
    "bipartite_component_size",
)
