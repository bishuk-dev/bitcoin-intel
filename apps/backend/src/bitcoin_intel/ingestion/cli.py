from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from bitcoin_intel.ingestion.errors import IngestionFileError
from bitcoin_intel.ingestion.pipeline import ingest_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bitcoin-intel")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser(
        "ingest", help="validate and normalize a CSV, JSON, or XML source into Parquet"
    )
    ingest_parser.add_argument("--input", type=Path, required=True, help="source file path")
    ingest_parser.add_argument("--output", type=Path, required=True, help="new dataset directory")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command != "ingest":
        raise AssertionError("argparse accepted an unknown command")
    try:
        summary = ingest_file(args.input, args.output)
    except (IngestionFileError, OSError) as error:
        print(f"Ingestion failed: {error}", file=sys.stderr)
        return 1

    print(f"Records read: {summary.records_read}")
    print(f"Accepted: {summary.records_accepted}")
    print(f"Rejected: {summary.records_rejected}")
    print(f"Unique transactions: {summary.unique_transactions}")
    print(f"Network observations: {summary.network_observations}")
    print(f"Output: {summary.output_path}")
    return 0
