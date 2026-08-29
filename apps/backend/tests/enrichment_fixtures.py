from __future__ import annotations

from pathlib import Path

from mmdb_writer import MMDBWriter
from netaddr import IPSet


def write_test_mmdb_resources(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    country = root / "DBIP-Country-Lite-Test.mmdb"
    asn = root / "DBIP-ASN-Lite-Test.mmdb"

    country_writer = MMDBWriter(
        ip_version=6,
        ipv4_compatible=True,
        database_type="DBIP-Country-Lite",
        languages=["en"],
        description={"en": "Purpose-built test country database"},
    )
    country_writer.insert_network(IPSet(["192.0.2.0/24"]), {"country": {"iso_code": "IN"}})
    country_writer.insert_network(IPSet(["198.51.100.0/24"]), {"country": {"iso_code": "US"}})
    country_writer.insert_network(IPSet(["2001:db8::/48"]), {"country": {"iso_code": "DE"}})
    country_writer.to_db_file(str(country))

    asn_writer = MMDBWriter(
        ip_version=6,
        ipv4_compatible=True,
        database_type="DBIP-ASN-Lite",
        languages=["en"],
        description={"en": "Purpose-built test ASN database"},
    )
    asn_writer.insert_network(
        IPSet(["192.0.2.0/24"]),
        {"autonomous_system_number": 64500, "autonomous_system_organization": "Test IN"},
    )
    asn_writer.insert_network(
        IPSet(["198.51.100.0/24"]),
        {"autonomous_system_number": 64501, "autonomous_system_organization": "Test US"},
    )
    asn_writer.insert_network(
        IPSet(["2001:db8::/48"]),
        {"autonomous_system_number": 64502, "autonomous_system_organization": "Test DE"},
    )
    asn_writer.to_db_file(str(asn))
    return country, asn
