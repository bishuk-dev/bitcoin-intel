from __future__ import annotations

from pathlib import Path

import pytest

from bitcoin_intel.benchmarking import SyntheticConfig, write_synthetic_json
from bitcoin_intel.ingestion import ingest_file


def test_synthetic_generator_is_deterministic_and_phase_1_valid(tmp_path: Path) -> None:
    config = SyntheticConfig(record_count=20, seed=7, duplicate_observation_ratio=0.25)
    first_source = tmp_path / "first.json"
    second_source = tmp_path / "second.json"

    first = write_synthetic_json(first_source, config)
    second = write_synthetic_json(second_source, config)

    assert first_source.read_bytes() == second_source.read_bytes()
    assert first == second
    assert first.unique_transaction_count == 15
    ingestion = ingest_file(first_source, tmp_path / "dataset")
    assert ingestion.records_accepted == 20
    assert ingestion.records_rejected == 0
    assert ingestion.unique_transactions == 15
    assert ingestion.network_observations == 20


def test_synthetic_seed_changes_generated_content(tmp_path: Path) -> None:
    first_source = tmp_path / "first.json"
    second_source = tmp_path / "second.json"
    write_synthetic_json(first_source, SyntheticConfig(record_count=5, seed=1))
    write_synthetic_json(second_source, SyntheticConfig(record_count=5, seed=2))

    assert first_source.read_bytes() != second_source.read_bytes()


@pytest.mark.parametrize(
    "config",
    [
        SyntheticConfig(record_count=1),
        SyntheticConfig(record_count=1, duplicate_observation_ratio=0),
        SyntheticConfig(record_count=1, ipv6_ratio=1),
    ],
)
def test_synthetic_config_boundaries_are_valid(config: SyntheticConfig) -> None:
    assert config.unique_transaction_count == 1
