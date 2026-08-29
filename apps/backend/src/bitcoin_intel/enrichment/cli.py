from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from bitcoin_intel.enrichment.pipeline import build_ip_enrichment
from bitcoin_intel.enrichment.validation import validate_enrichment_store


def configure_enrichment_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("enrichment", help="build or validate offline IP enrichment")
    commands = parser.add_subparsers(dest="enrichment_command", required=True)
    build = commands.add_parser("build", help="build a new atomic IP enrichment store")
    build.add_argument("--dataset", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--country-db", type=Path, required=True)
    build.add_argument("--asn-db", type=Path, required=True)
    validate = commands.add_parser("validate", help="validate enrichment data and lineage")
    validate.add_argument("--enrichment", type=Path, required=True)
    validate.add_argument("--dataset", type=Path, required=True)


def run_enrichment_command(args: argparse.Namespace) -> int:
    if args.enrichment_command == "build":
        summary = build_ip_enrichment(args.dataset, args.output, args.country_db, args.asn_db)
        print(json.dumps(asdict(summary), indent=2, sort_keys=True, default=str))
        return 0
    if args.enrichment_command == "validate":
        report = validate_enrichment_store(args.enrichment, args.dataset)
        print(
            json.dumps(
                {"valid": report.is_valid, "issues": [asdict(issue) for issue in report.issues]},
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report.is_valid else 2
    raise AssertionError("argparse accepted an unknown enrichment command")
