from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bitcoin_intel.analytics.dataset import AnalyticalDataset, AnalyticalDatasetError
from bitcoin_intel.analytics.queries import AnalyticalQueries, TimeBucket
from bitcoin_intel.analytics.validation import validate_analytical_dataset


def configure_analytics_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    analytics_parser = subparsers.add_parser(
        "analytics", help="query or validate a canonical Parquet dataset with embedded DuckDB"
    )
    commands = analytics_parser.add_subparsers(dest="analytics_command", required=True)

    validate_parser = commands.add_parser("validate", help="run analytical integrity checks")
    _add_dataset_argument(validate_parser)

    transaction_parser = commands.add_parser("tx", help="look up one transaction by TXID")
    _add_dataset_argument(transaction_parser)
    transaction_parser.add_argument("--txid", required=True)

    address_parser = commands.add_parser("address", help="summarize one address")
    _add_dataset_argument(address_parser)
    address_parser.add_argument("--address", required=True)

    high_value_parser = commands.add_parser(
        "high-value", help="rank transactions by total output satoshis"
    )
    _add_dataset_argument(high_value_parser)
    high_value_parser.add_argument("--limit", type=int, default=20)

    high_fee_parser = commands.add_parser("high-fee", help="rank transactions by fee satoshis")
    _add_dataset_argument(high_fee_parser)
    high_fee_parser.add_argument("--limit", type=int, default=20)

    ip_parser = commands.add_parser("ip", help="summarize observations involving one IP")
    _add_dataset_argument(ip_parser)
    ip_parser.add_argument("--ip", required=True)

    asn_parser = commands.add_parser("asn", help="summarize one source-reported ASN")
    _add_dataset_argument(asn_parser)
    asn_parser.add_argument("--asn", type=int, required=True)

    temporal_parser = commands.add_parser("temporal", help="aggregate observations by UTC bucket")
    _add_dataset_argument(temporal_parser)
    temporal_parser.add_argument("--bucket", choices=("hour", "day"), required=True)
    temporal_parser.add_argument("--start", type=_datetime_argument)
    temporal_parser.add_argument("--end", type=_datetime_argument)


def run_analytics_command(args: argparse.Namespace) -> int:
    dataset = AnalyticalDataset(args.dataset)
    if args.analytics_command == "validate":
        report = validate_analytical_dataset(dataset)
        _print_json(
            {
                "valid": report.is_valid,
                "issues": [asdict(issue) for issue in report.issues],
            }
        )
        return 0 if report.is_valid else 2

    try:
        with dataset.connect() as connection:
            queries = AnalyticalQueries(connection)
            result = _execute_query(args, queries)
    except duckdb.Error as error:
        raise AnalyticalDatasetError(f"analytical query failed: {error}") from error
    _print_json(result)
    return 0


def _execute_query(args: argparse.Namespace, queries: AnalyticalQueries) -> Any:
    if args.analytics_command == "tx":
        result = queries.transaction(args.txid)
        return None if result is None else asdict(result)
    if args.analytics_command == "address":
        return asdict(queries.address_activity(args.address))
    if args.analytics_command == "high-value":
        return [asdict(item) for item in queries.high_value_transactions(limit=args.limit)]
    if args.analytics_command == "high-fee":
        return [asdict(item) for item in queries.high_fee_transactions(limit=args.limit)]
    if args.analytics_command == "ip":
        return asdict(queries.ip_activity(args.ip))
    if args.analytics_command == "asn":
        return asdict(queries.asn_activity(args.asn))
    if args.analytics_command == "temporal":
        bucket: TimeBucket = args.bucket
        return [
            asdict(item)
            for item in queries.temporal_activity(bucket, start=args.start, end=args.end)
        ]
    raise AssertionError("argparse accepted an unknown analytics command")


def _add_dataset_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", type=Path, required=True)


def _datetime_argument(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be valid ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include an explicit timezone")
    return parsed.astimezone(UTC)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=_json_default))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError(f"cannot serialize value of type {type(value).__name__}")
