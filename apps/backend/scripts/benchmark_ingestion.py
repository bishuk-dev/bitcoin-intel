from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter

from bitcoin_intel.ingestion import ingest_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a non-CI synthetic JSON ingestion performance sanity check."
    )
    parser.add_argument("--records", type=int, default=10_000)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.records <= 0:
        raise SystemExit("--records must be a positive integer")

    with tempfile.TemporaryDirectory(prefix="bitcoin-intel-benchmark-") as temporary:
        work_directory = Path(temporary)
        source_path = work_directory / "synthetic.json"
        output_path = work_directory / "dataset"
        _write_source(source_path, args.records)

        source_size = source_path.stat().st_size
        started = perf_counter()
        summary = ingest_file(source_path, output_path)
        elapsed = perf_counter() - started
        output_size = sum(path.stat().st_size for path in output_path.rglob("*") if path.is_file())

        print(f"records={summary.records_read}")
        print("format=json")
        print(f"input_bytes={source_size}")
        print(f"elapsed_seconds={elapsed:.6f}")
        print(f"output_bytes={output_size}")
        print(f"accepted={summary.records_accepted}")
        print(f"rejected={summary.records_rejected}")
    return 0


def _write_source(path: Path, record_count: int) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as source:
        source.write("[\n")
        for index in range(record_count):
            if index:
                source.write(",\n")
            record = {
                "timestamp": "2026-08-28T12:30:00Z",
                "src_ip": "192.0.2.1",
                "dst_ip": "2001:db8::1",
                "src_port": 8333,
                "dst_port": 10_000 + (index % 50_000),
                "txid": f"{index:064x}",
                "input_addresses": [f"input-{index}"],
                "output_addresses": [f"output-{index}"],
                "input_amounts": [1],
                "output_amounts": [1],
                "fee": 0,
                "script_type": "synthetic",
                "geo_country": "IN",
                "asn": 64_512,
            }
            json.dump(record, source, separators=(",", ":"), sort_keys=True)
        source.write("\n]\n")


if __name__ == "__main__":
    raise SystemExit(main())
