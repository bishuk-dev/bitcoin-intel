from __future__ import annotations

import json
from pathlib import Path

import maxminddb
import pyarrow.parquet as pq
import pytest

from bitcoin_intel.enrichment.pipeline import EnrichmentBuildError, build_ip_enrichment
from bitcoin_intel.enrichment.resources import GeoIPResourceError
from bitcoin_intel.enrichment.validation import validate_enrichment_store
from tests.enrichment_fixtures import write_test_mmdb_resources
from tests.feature_fixtures import create_feature_dataset


def test_build_enriches_ipv4_ipv6_and_preserves_explicit_misses(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    output = tmp_path / "enrichment"

    summary = build_ip_enrichment(dataset, output, country, asn)
    rows = {
        row["ip"]: row
        for row in pq.read_table(output / "ip_enrichment" / "part-00000.parquet").to_pylist()
    }

    assert summary.canonical_ip_count == len(rows) == 10
    assert rows["192.0.2.10"]["enriched_country_code"] == "IN"
    assert rows["192.0.2.10"]["enriched_asn"] == 64500
    assert rows["192.0.2.10"]["country_network"] == "192.0.2.0/24"
    assert rows["2001:db8::1"]["enriched_country_code"] == "DE"
    assert rows["2001:db8::1"]["asn_network"] == "2001:db8::/48"
    assert rows["203.0.113.5"]["enriched_country_code"] is None
    assert rows["203.0.113.5"]["enriched_asn"] is None
    assert not rows["203.0.113.5"]["country_found"]
    assert rows["203.0.113.5"]["is_private"]
    assert not rows["203.0.113.5"]["is_global"]
    assert validate_enrichment_store(output, dataset).is_valid


def test_readers_are_opened_once_and_reused_for_all_ips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    original = maxminddb.open_database
    opened: list[Path] = []

    def counting_open(path: Path) -> object:
        opened.append(Path(path))
        return original(path)

    monkeypatch.setattr(maxminddb, "open_database", counting_open)
    build_ip_enrichment(dataset, tmp_path / "enrichment", country, asn)
    assert opened == [country.resolve(), asn.resolve()]


@pytest.mark.parametrize("kind", ["missing", "empty", "corrupt"])
def test_invalid_database_fails_before_publication(tmp_path: Path, kind: str) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    bad = tmp_path / f"{kind}.mmdb"
    if kind == "empty":
        bad.touch()
    elif kind == "corrupt":
        bad.write_bytes(b"not an MMDB")
    with pytest.raises(GeoIPResourceError):
        build_ip_enrichment(dataset, tmp_path / "output", bad, asn)
    assert not (tmp_path / "output").exists()
    assert country.is_file()


def test_wrong_database_role_is_rejected(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    with pytest.raises(GeoIPResourceError, match="incompatible database type"):
        build_ip_enrichment(dataset, tmp_path / "output", asn, country)


def test_build_is_deterministic_and_never_overwrites(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    first, second = tmp_path / "first", tmp_path / "second"
    first_summary = build_ip_enrichment(dataset, first, country, asn)
    second_summary = build_ip_enrichment(dataset, second, country, asn)
    assert first_summary.enrichment_dataset_id == second_summary.enrichment_dataset_id
    assert (first / "ip_enrichment" / "part-00000.parquet").read_bytes() == (
        second / "ip_enrichment" / "part-00000.parquet"
    ).read_bytes()
    with pytest.raises(EnrichmentBuildError, match="will not be overwritten"):
        build_ip_enrichment(dataset, first, country, asn)


def test_validation_detects_manifest_resource_hash_tampering(tmp_path: Path) -> None:
    dataset = create_feature_dataset(tmp_path)
    country, asn = write_test_mmdb_resources(tmp_path / "resources")
    output = tmp_path / "enrichment"
    build_ip_enrichment(dataset, output, country, asn)
    manifest_path = output / "enrichment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resources"]["country"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = validate_enrichment_store(output, dataset)
    assert not report.is_valid
    assert {issue.code for issue in report.issues} == {"ENRICHMENT_DATASET_ID_MISMATCH"}
