from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitcoin_intel.analytics.dataset import (
    CANONICAL_TABLES,
    AnalyticalDataset,
    AnalyticalDatasetError,
)


def test_valid_dataset_registers_canonical_and_summary_views(
    analytical_dataset_path: Path,
) -> None:
    dataset = AnalyticalDataset(analytical_dataset_path)

    with dataset.connect() as connection:
        views = {
            str(row[0])
            for row in connection.execute(
                """SELECT table_name FROM information_schema.views
                WHERE table_schema = 'main'"""
            ).fetchall()
        }
        summary_schema = {
            str(row[0]): str(row[1])
            for row in connection.execute("DESCRIBE transaction_summary").fetchall()
        }

    assert set(CANONICAL_TABLES) <= views
    assert "transaction_summary" in views
    assert summary_schema["total_input_sats"] == "HUGEINT"
    assert summary_schema["total_output_sats"] == "HUGEINT"
    assert dataset.manifest.schema_version == "1.0.0"


def test_missing_dataset_fails(tmp_path: Path) -> None:
    with pytest.raises(AnalyticalDatasetError, match="does not exist"):
        AnalyticalDataset(tmp_path / "missing")


def test_missing_manifest_fails(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()

    with pytest.raises(AnalyticalDatasetError, match="manifest is missing"):
        AnalyticalDataset(dataset)


def test_unsupported_schema_version_fails(analytical_dataset_path: Path) -> None:
    manifest_path = analytical_dataset_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AnalyticalDatasetError, match="unsupported dataset schema version"):
        AnalyticalDataset(analytical_dataset_path)


def test_missing_canonical_table_fails(analytical_dataset_path: Path) -> None:
    missing_file = analytical_dataset_path / "transactions" / "part-00000.parquet"
    missing_file.unlink()

    with pytest.raises(AnalyticalDatasetError, match="missing or escapes"):
        AnalyticalDataset(analytical_dataset_path)


def test_manifest_cannot_redirect_a_table_path(analytical_dataset_path: Path) -> None:
    manifest_path = analytical_dataset_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["output_tables"]["transactions"]["file"] = "../outside.parquet"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AnalyticalDatasetError, match="unsupported Parquet layout"):
        AnalyticalDataset(analytical_dataset_path)
