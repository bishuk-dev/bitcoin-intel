from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from bitcoin_intel.analytics.cli import configure_analytics_parser, run_analytics_command
from bitcoin_intel.analytics.dataset import AnalyticalDatasetError
from bitcoin_intel.enrichment.cli import configure_enrichment_parser, run_enrichment_command
from bitcoin_intel.enrichment.pipeline import EnrichmentBuildError
from bitcoin_intel.enrichment.resources import GeoIPResourceError
from bitcoin_intel.enrichment.validation import EnrichmentStoreError
from bitcoin_intel.features.cli import configure_features_parser, run_features_command
from bitcoin_intel.features.pipeline import FeatureBuildError
from bitcoin_intel.features.validation import FeatureStoreError
from bitcoin_intel.graph.cli import configure_graph_parser, run_graph_command
from bitcoin_intel.graph.connection import GraphConnectionError
from bitcoin_intel.graph.docker import GraphRebuildError
from bitcoin_intel.graph.import_builder import GraphImportError
from bitcoin_intel.ingestion.errors import IngestionFileError
from bitcoin_intel.ingestion.pipeline import ingest_file
from bitcoin_intel.ml.cli import configure_ml_parser, run_ml_command
from bitcoin_intel.ml.models import MLExperimentError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bitcoin-intel")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser(
        "ingest", help="validate and normalize a CSV, JSON, or XML source into Parquet"
    )
    ingest_parser.add_argument("--input", type=Path, required=True, help="source file path")
    ingest_parser.add_argument("--output", type=Path, required=True, help="new dataset directory")
    configure_analytics_parser(subparsers)
    configure_enrichment_parser(subparsers)
    configure_graph_parser(subparsers)
    configure_features_parser(subparsers)
    configure_ml_parser(subparsers)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "analytics":
        try:
            return run_analytics_command(args)
        except (AnalyticalDatasetError, ValueError, OSError) as error:
            print(f"Analytics failed: {error}", file=sys.stderr)
            return 1
    if args.command == "enrichment":
        try:
            return run_enrichment_command(args)
        except (
            AnalyticalDatasetError,
            EnrichmentBuildError,
            EnrichmentStoreError,
            GeoIPResourceError,
            ValueError,
            OSError,
        ) as error:
            print(f"Enrichment operation failed: {error}", file=sys.stderr)
            return 1
    if args.command == "graph":
        try:
            return run_graph_command(args)
        except (
            AnalyticalDatasetError,
            GraphConnectionError,
            GraphImportError,
            GraphRebuildError,
            ValueError,
            OSError,
        ) as error:
            print(f"Graph operation failed: {error}", file=sys.stderr)
            return 1
    if args.command == "features":
        try:
            return run_features_command(args)
        except (
            AnalyticalDatasetError,
            FeatureBuildError,
            FeatureStoreError,
            ValueError,
            OSError,
        ) as error:
            print(f"Feature operation failed: {error}", file=sys.stderr)
            return 1
    if args.command == "ml":
        try:
            return run_ml_command(args)
        except (MLExperimentError, ValueError, OSError) as error:
            print(f"ML experiment failed: {error}", file=sys.stderr)
            return 1
    if args.command != "ingest":
        raise AssertionError("argparse accepted an unknown top-level command")
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
