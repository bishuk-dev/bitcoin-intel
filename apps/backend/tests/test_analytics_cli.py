from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitcoin_intel.ingestion.cli import main as cli_main


def test_analytics_validate_cli_reports_valid_dataset(
    analytical_dataset_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(["analytics", "validate", "--dataset", str(analytical_dataset_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {"issues": [], "valid": True}


def test_analytics_address_cli_returns_typed_result_as_json(
    analytical_dataset_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(
        [
            "analytics",
            "address",
            "--dataset",
            str(analytical_dataset_path),
            "--address",
            "BothAddress",
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert result["transaction_count"] == 2
    assert result["total_output_sats"] == 290_000_000


def test_analytics_cli_returns_nonzero_for_invalid_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(["analytics", "validate", "--dataset", str(tmp_path / "missing")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Analytics failed:" in captured.err
