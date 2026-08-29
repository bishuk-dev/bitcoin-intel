from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EXPERIMENT_SCHEMA_VERSION = "1.0.0"
ENTITY_TYPE = "transaction"
ENTITY_ID_COLUMN = "txid"
TIME_COLUMN = "first_observed_at"

TRANSACTION_FEATURES = (
    "input_count",
    "output_count",
    "total_input_sats",
    "total_output_sats",
    "fee_sats",
    "mean_input_sats",
    "mean_output_sats",
    "max_input_sats",
    "max_output_sats",
    "min_input_sats",
    "min_output_sats",
    "input_value_std",
    "output_value_std",
    "fee_to_input_ratio",
)
NETWORK_FEATURES = (
    "network_observation_count",
    "unique_source_ip_count",
    "unique_destination_ip_count",
    "unique_ip_count",
    "unique_reported_asn_count",
    "unique_reported_country_count",
)
TEMPORAL_FEATURES = (
    "observation_span_seconds",
    "mean_inter_observation_seconds",
    "median_inter_observation_seconds",
    "min_inter_observation_seconds",
    "max_inter_observation_seconds",
    "active_hour_count",
    "observations_per_active_hour",
    "hour_of_day_entropy",
    "day_activity_entropy",
    "max_observations_1m",
    "max_observations_5m",
    "max_observations_1h",
)
ENRICHMENT_FEATURES = (
    "unique_enriched_country_count",
    "unique_enriched_asn_count",
    "source_destination_country_match_rate",
    "source_destination_asn_match_rate",
)
FEATURE_FAMILIES = {
    "transaction-only": TRANSACTION_FEATURES,
    "network-only": NETWORK_FEATURES + TEMPORAL_FEATURES,
    "transaction-temporal": TRANSACTION_FEATURES + TEMPORAL_FEATURES,
    "transaction-network": TRANSACTION_FEATURES + NETWORK_FEATURES,
    "transaction-network-enrichment": (
        TRANSACTION_FEATURES + NETWORK_FEATURES + ENRICHMENT_FEATURES
    ),
    "cross-layer": TRANSACTION_FEATURES + NETWORK_FEATURES + ENRICHMENT_FEATURES,
    "all-eligible": (
        TRANSACTION_FEATURES + TEMPORAL_FEATURES + NETWORK_FEATURES + ENRICHMENT_FEATURES
    ),
}
EXPERIMENT_MODELS = {
    "anomaly": ("isolation-forest", "local-outlier-factor", "pca-reconstruction"),
    "scenario": (
        "logistic-regression",
        "random-forest",
        "hist-gradient-boosting",
        "xgboost",
        "lightgbm",
    ),
}
SPLIT_STRATEGIES = ("random-stratified", "group", "temporal")
CALIBRATION_METHODS = ("none", "sigmoid")
ParameterValue = bool | int | float | str | None


class MLExperimentError(RuntimeError):
    """Raised when an ML experiment cannot be constructed or verified safely."""


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    feature_path: Path
    output_root: Path
    experiment_type: str
    model: str
    truth_path: Path | None = None
    entity_type: str = ENTITY_TYPE
    feature_family: str = "all-eligible"
    split_strategy: str = "group"
    seed: int = 42
    calibration: str = "none"
    parameter_overrides: tuple[tuple[str, ParameterValue], ...] = ()

    def __post_init__(self) -> None:
        if self.experiment_type not in EXPERIMENT_MODELS:
            raise ValueError(f"unsupported experiment type: {self.experiment_type}")
        if self.model not in EXPERIMENT_MODELS[self.experiment_type]:
            raise ValueError(
                f"model {self.model!r} is not valid for {self.experiment_type!r} experiments"
            )
        if self.entity_type != ENTITY_TYPE:
            raise ValueError("Phase 5 supports transaction experiments only")
        if self.feature_family not in FEATURE_FAMILIES:
            raise ValueError(f"unsupported transaction feature family: {self.feature_family}")
        if self.split_strategy not in SPLIT_STRATEGIES:
            raise ValueError(f"unsupported split strategy: {self.split_strategy}")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if self.calibration not in CALIBRATION_METHODS:
            raise ValueError(f"unsupported calibration method: {self.calibration}")
        if self.experiment_type != "scenario" and self.calibration != "none":
            raise ValueError("probability calibration applies to scenario classifiers only")
        names = [name for name, _ in self.parameter_overrides]
        if len(names) != len(set(names)):
            raise ValueError("model parameter overrides contain duplicate names")
        if self.experiment_type == "scenario" and self.truth_path is None:
            raise ValueError("scenario experiments require an evaluation truth sidecar")
        if self.split_strategy == "group" and self.truth_path is None:
            raise ValueError("group-aware splitting requires an evaluation truth sidecar")


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
    output_path: Path
    experiment_id: str
    experiment_type: str
    model: str
    rows: int
    feature_count: int
    split_counts: dict[str, int]
    primary_metric_name: str
    primary_metric_value: float | None
