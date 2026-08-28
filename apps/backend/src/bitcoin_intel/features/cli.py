from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bitcoin_intel.features.models import FeatureBuildConfig
from bitcoin_intel.features.pipeline import build_features
from bitcoin_intel.features.validation import validate_feature_store


def configure_features_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "features", help="build or validate deterministic Parquet feature datasets"
    )
    commands = parser.add_subparsers(dest="features_command", required=True)
    build = commands.add_parser("build", help="build a new atomic feature dataset")
    build.add_argument("--dataset", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--cutoff", type=_datetime_argument)
    build.add_argument("--reused-ip-min-transactions", type=int, default=2)
    validate = commands.add_parser("validate", help="validate a feature dataset and lineage")
    validate.add_argument("--features", type=Path, required=True)
    validate.add_argument("--dataset", type=Path, required=True)


def run_features_command(args: argparse.Namespace) -> int:
    if args.features_command == "build":
        summary = build_features(
            args.dataset,
            args.output,
            FeatureBuildConfig(
                cutoff=args.cutoff,
                reused_ip_min_transactions=args.reused_ip_min_transactions,
            ),
        )
        print(json.dumps(asdict(summary), indent=2, sort_keys=True, default=_json_default))
        return 0
    if args.features_command == "validate":
        report = validate_feature_store(args.features, args.dataset)
        print(
            json.dumps(
                {"valid": report.is_valid, "issues": [asdict(issue) for issue in report.issues]},
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report.is_valid else 2
    raise AssertionError("argparse accepted an unknown features command")


def _datetime_argument(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("cutoff must be valid ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include an explicit timezone")
    return parsed.astimezone(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError(f"cannot serialize {type(value).__name__}")
