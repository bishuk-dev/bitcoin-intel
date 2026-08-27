from __future__ import annotations

from typing import Any


def make_source_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": "2026-08-28T12:30:00Z",
        "src_ip": "192.0.2.1",
        "dst_ip": "198.51.100.2",
        "src_port": 8333,
        "dst_port": 49152,
        "txid": "a" * 64,
        "input_addresses": ["InputAddressA"],
        "output_addresses": ["OutputAddressA"],
        "input_amounts": ["1.0"],
        "output_amounts": ["0.99"],
        "fee": "0.01",
        "script_type": "p2wpkh",
        "geo_country": "IN",
        "asn": 64512,
    }
    record.update(overrides)
    return record
