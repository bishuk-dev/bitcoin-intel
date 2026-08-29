from __future__ import annotations

from pathlib import Path

from bitcoin_intel.ingestion.cli import main as cli_main
from tests.enrichment_fixtures import write_test_mmdb_resources
from tests.feature_fixtures import create_feature_dataset


def test_feature_build_and_validate_cli(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    enrichment = tmp_path / "enrichment"
    assert (
        cli_main(
            [
                "enrichment",
                "build",
                "--dataset",
                str(dataset),
                "--country-db",
                str(country),
                "--asn-db",
                str(asn),
                "--output",
                str(enrichment),
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "enrichment",
                "validate",
                "--dataset",
                str(dataset),
                "--enrichment",
                str(enrichment),
            ]
        )
        == 0
    )
    output = tmp_path / "features"
    assert (
        cli_main(
            [
                "features",
                "build",
                "--dataset",
                str(dataset),
                "--enrichment",
                str(enrichment),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "features",
                "validate",
                "--dataset",
                str(dataset),
                "--enrichment",
                str(enrichment),
                "--features",
                str(output),
            ]
        )
        == 0
    )


def test_feature_cli_rejects_naive_cutoff(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    enrichment = tmp_path / "enrichment"
    assert (
        cli_main(
            [
                "enrichment",
                "build",
                "--dataset",
                str(dataset),
                "--country-db",
                str(country),
                "--asn-db",
                str(asn),
                "--output",
                str(enrichment),
            ]
        )
        == 0
    )
    output = tmp_path / "features"
    try:
        cli_main(
            [
                "features",
                "build",
                "--dataset",
                str(dataset),
                "--enrichment",
                str(enrichment),
                "--output",
                str(output),
                "--cutoff",
                "2026-01-01T12:00:00",
            ]
        )
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("argparse should reject a timezone-naive cutoff")
