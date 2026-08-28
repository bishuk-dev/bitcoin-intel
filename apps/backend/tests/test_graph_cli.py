from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitcoin_intel.ingestion.cli import build_parser
from bitcoin_intel.ingestion.cli import main as cli_main


def test_graph_prepare_and_validate_import_cli_do_not_require_neo4j(
    analytical_dataset_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "graph-import"

    prepare_code = cli_main(
        [
            "graph",
            "prepare",
            "--dataset",
            str(analytical_dataset_path),
            "--output",
            str(output),
        ]
    )
    prepared = json.loads(capsys.readouterr().out)
    validate_code = cli_main(
        [
            "graph",
            "validate-import",
            "--input",
            str(output),
            "--dataset",
            str(analytical_dataset_path),
        ]
    )
    validated = json.loads(capsys.readouterr().out)

    assert prepare_code == 0
    assert prepared["node_counts"]["network_observations"] == 4
    assert validate_code == 0
    assert validated == {"issues": [], "valid": True}


def test_graph_cli_exposes_only_bounded_foundational_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    graph_args = parser.parse_args(
        [
            "graph",
            "path",
            "--source-kind",
            "address",
            "--source-id",
            "A",
            "--target-kind",
            "transaction",
            "--target-id",
            "a" * 64,
            "--max-depth",
            "8",
        ]
    )

    assert "graph" in help_text
    assert graph_args.max_depth == 8
    assert not hasattr(graph_args, "cypher")


def test_graph_prepare_cli_reports_existing_destination_without_overwrite(
    analytical_dataset_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "graph-import"
    output.mkdir()

    exit_code = cli_main(
        [
            "graph",
            "prepare",
            "--dataset",
            str(analytical_dataset_path),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "already exists" in captured.err
