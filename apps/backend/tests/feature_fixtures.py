from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from bitcoin_intel.ingestion import ingest_file
from tests.factories import make_source_record


def create_feature_dataset(root: Path) -> Path:
    tx1 = {
        "txid": "1" * 64,
        "input_addresses": ["AddressA", "InputX", "InputY"],
        "input_amounts": ["3", "2", "1"],
        "output_addresses": ["AddressA", "OutputB", "OutputC", "OutputD"],
        "output_amounts": ["1", "1", "1", "2.9"],
        "fee": "0.1",
        "script_type": "p2wpkh",
    }
    observations = [
        ("2026-01-01T12:00:00Z", "192.0.2.10", "192.0.2.10", 8333, 18444),
        ("2026-01-01T12:00:00Z", "192.0.2.10", "2001:db8::1", 8333, 20000),
        ("2026-01-01T12:00:30Z", "192.0.2.10", "2001:db8::2", 8333, 20001),
        ("2026-01-01T12:04:00Z", "198.51.100.2", "2001:db8::3", 18444, 20002),
        ("2026-01-01T13:00:00Z", "198.51.100.3", "2001:db8::4", 18444, 20003),
    ]
    records = [
        make_source_record(
            **tx1,
            timestamp=timestamp,
            src_ip=src_ip,
            dst_ip=dst_ip,
            src_port=src_port,
            dst_port=dst_port,
            geo_country="IN" if index < 3 else "US",
            asn=64512 if index < 3 else 64513,
        )
        for index, (timestamp, src_ip, dst_ip, src_port, dst_port) in enumerate(observations)
    ]
    records.extend(
        [
            make_source_record(
                txid="2" * 64,
                timestamp="2026-01-01T14:00:00Z",
                src_ip="192.0.2.10",
                dst_ip="203.0.113.5",
                input_addresses=["AddressA"],
                input_amounts=["1"],
                output_addresses=["OutputE"],
                output_amounts=["0.99"],
                fee="0.01",
            ),
            make_source_record(
                txid="3" * 64,
                timestamp="2026-01-01T11:00:00+00:00",
                src_ip="2001:db8:1::1",
                dst_ip="203.0.113.9",
                input_addresses=["DisconnectedF"],
                input_amounts=["2"],
                output_addresses=["DisconnectedG"],
                output_amounts=["1.99"],
                fee="0.01",
            ),
        ]
    )
    source = root / "features-source.json"
    source.write_text(json.dumps(records), encoding="utf-8")
    dataset = root / "dataset"
    ingest_file(source, dataset)
    return dataset


def read_feature_rows(feature_store: Path, table_name: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return cast(
        list[dict[str, Any]],
        pq.read_table(feature_store / table_name / "part-00000.parquet").to_pylist(),
    )
