from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bitcoin_intel.ingestion import ingest_file
from tests.factories import make_source_record


def create_analytical_dataset(root: Path) -> Path:
    source = root / "analytical.json"
    transaction_a = {
        "txid": "a" * 64,
        "input_addresses": ["InputOnly", "SharedMulti"],
        "input_amounts": ["1", "2"],
        "output_addresses": ["OutputOnly", "BothAddress"],
        "output_amounts": ["0.5", "2.4"],
        "fee": "0.1",
        "script_type": "p2wpkh",
    }
    records = [
        make_source_record(
            **transaction_a,
            timestamp="2026-08-28T23:30:00Z",
            src_ip="192.0.2.1",
            dst_ip="2001:db8::1",
            src_port=8333,
            dst_port=49152,
            geo_country="IN",
            asn=64512,
        ),
        make_source_record(
            **transaction_a,
            timestamp="2026-08-29T00:15:00Z",
            src_ip="2001:db8::2",
            dst_ip="192.0.2.1",
            src_port=18444,
            dst_port=8333,
            geo_country=None,
            asn=None,
        ),
        make_source_record(
            txid="b" * 64,
            timestamp="2026-08-29T01:00:00Z",
            src_ip="192.0.2.1",
            dst_ip="198.51.100.2",
            src_port=8333,
            dst_port=50000,
            input_addresses=["BothAddress", "SharedMulti"],
            input_amounts=["1", "3"],
            output_addresses=["BothAddress", "SecondOutput"],
            output_amounts=["0.5", "3.4"],
            fee="0.1",
            script_type="p2tr",
            geo_country="US",
            asn=64512,
        ),
        make_source_record(
            txid="c" * 64,
            timestamp="2026-08-30T12:00:00Z",
            src_ip="203.0.113.9",
            dst_ip="2001:db8::1",
            src_port=8333,
            dst_port=8333,
            input_addresses=["SharedMulti"],
            input_amounts=["5"],
            output_addresses=["ThirdOutput"],
            output_amounts=["4.8"],
            fee="0.2",
            script_type=None,
            geo_country="DE",
            asn=64513,
        ),
    ]
    write_json_records(source, records)
    dataset = root / "dataset"
    ingest_file(source, dataset)
    return dataset


def write_json_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(records), encoding="utf-8")
