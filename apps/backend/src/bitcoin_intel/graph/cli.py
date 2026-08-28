from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.graph.config import GraphSettings
from bitcoin_intel.graph.connection import GraphConnection
from bitcoin_intel.graph.docker import rebuild_graph
from bitcoin_intel.graph.gds import verify_gds_foundation
from bitcoin_intel.graph.import_builder import prepare_graph_import, validate_graph_import
from bitcoin_intel.graph.models import GraphImportManifest, GraphNodeIdentity
from bitcoin_intel.graph.queries import GraphQueries
from bitcoin_intel.graph.schema import ensure_graph_constraints, plugin_versions
from bitcoin_intel.graph.validation import validate_graph


def configure_graph_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    graph_parser = subparsers.add_parser(
        "graph", help="prepare, rebuild, validate, or query the derived Neo4j graph"
    )
    commands = graph_parser.add_subparsers(dest="graph_command", required=True)

    prepare = commands.add_parser("prepare", help="derive strict Neo4j import Parquet")
    _dataset_argument(prepare)
    prepare.add_argument("--output", type=Path, required=True)

    validate_import = commands.add_parser(
        "validate-import", help="validate derived graph-import Parquet"
    )
    validate_import.add_argument("--input", type=Path, required=True)
    validate_import.add_argument("--dataset", type=Path)

    rebuild = commands.add_parser(
        "rebuild", help="DESTRUCTIVELY replace Neo4j from canonical Parquet"
    )
    _dataset_argument(rebuild)
    rebuild.add_argument("--output", type=Path, required=True)
    rebuild.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    rebuild.add_argument(
        "--compose-project-name",
        help="optional isolated Docker Compose project name for the database volume",
    )
    rebuild.add_argument("--confirm-replace-database", action="store_true")
    rebuild.add_argument("--max-off-heap-memory", default="1G")
    rebuild.add_argument("--import-threads", type=int, default=2)

    commands.add_parser("health", help="verify Neo4j, GDS, and APOC versions")
    commands.add_parser("ensure-schema", help="create the four named identity constraints")

    validate = commands.add_parser("validate", help="compare live graph to canonical Parquet")
    _dataset_argument(validate)

    transaction = commands.add_parser("tx", help="return a bounded transaction neighborhood")
    transaction.add_argument("--txid", required=True)

    address = commands.add_parser("address", help="return factual uses of one address")
    address.add_argument("--address", required=True)

    ip = commands.add_parser("ip", help="return observations involving one IP address")
    ip.add_argument("--ip", required=True)

    path = commands.add_parser("path", help="return a bounded connectivity-only shortest path")
    kinds = ("transaction", "address", "ip", "observation")
    path.add_argument("--source-kind", choices=kinds, required=True)
    path.add_argument("--source-id", required=True)
    path.add_argument("--target-kind", choices=kinds, required=True)
    path.add_argument("--target-id", required=True)
    path.add_argument("--max-depth", type=int, default=4)

    commands.add_parser("gds-verify", help="estimate/project and run read-only WCC validation")


def run_graph_command(args: argparse.Namespace) -> int:
    if args.graph_command == "prepare":
        prepared = prepare_graph_import(args.dataset, args.output)
        _print_json(_prepared_result(prepared.manifest, prepared.path))
        return 0
    if args.graph_command == "validate-import":
        dataset = None if args.dataset is None else AnalyticalDataset(args.dataset)
        import_report = validate_graph_import(args.input, canonical_dataset=dataset)
        _print_json(
            {
                "valid": import_report.is_valid,
                "issues": [asdict(item) for item in import_report.issues],
            }
        )
        return 0 if import_report.is_valid else 2

    settings = GraphSettings()  # type: ignore[call-arg]
    if args.graph_command == "rebuild":
        rebuild_result = rebuild_graph(
            dataset_path=args.dataset,
            output_path=args.output,
            compose_file=args.compose_file,
            settings=settings,
            confirm_replace_database=args.confirm_replace_database,
            compose_project_name=args.compose_project_name,
            max_off_heap_memory=args.max_off_heap_memory,
            import_threads=args.import_threads,
        )
        _print_json(
            {
                "import_path": str(rebuild_result.prepared.path),
                "dry_run_seconds": rebuild_result.dry_run_seconds,
                "import_seconds": rebuild_result.import_seconds,
                "counts": asdict(rebuild_result.integrity.graph_counts),
                "valid": rebuild_result.integrity.is_valid,
            }
        )
        return 0

    with GraphConnection(settings).connect() as driver:
        command = args.graph_command
        if command == "health":
            _print_json(asdict(plugin_versions(driver, settings.neo4j_database)))
            return 0
        if command == "ensure-schema":
            names = ensure_graph_constraints(driver, settings.neo4j_database)
            _print_json({"constraints": names})
            return 0
        if command == "validate":
            integrity_report = validate_graph(
                driver, settings.neo4j_database, AnalyticalDataset(args.dataset)
            )
            _print_json(
                {
                    "valid": integrity_report.is_valid,
                    "graph_counts": asdict(integrity_report.graph_counts),
                    "canonical_counts": asdict(integrity_report.canonical_counts),
                    "issues": [asdict(item) for item in integrity_report.issues],
                }
            )
            return 0 if integrity_report.is_valid else 2
        queries = GraphQueries(driver, settings.neo4j_database)
        if command == "tx":
            transaction_result = queries.transaction_neighborhood(args.txid)
            _print_json(None if transaction_result is None else asdict(transaction_result))
            return 0
        if command == "address":
            _print_json(asdict(queries.address_transactions(args.address)))
            return 0
        if command == "ip":
            _print_json(asdict(queries.ip_observations(args.ip)))
            return 0
        if command == "path":
            path_result = queries.shortest_path(
                GraphNodeIdentity(args.source_kind, args.source_id),
                GraphNodeIdentity(args.target_kind, args.target_id),
                max_depth=args.max_depth,
            )
            _print_json(None if path_result is None else asdict(path_result))
            return 0
        if command == "gds-verify":
            _print_json(asdict(verify_gds_foundation(driver, settings.neo4j_database)))
            return 0
    raise AssertionError("argparse accepted an unknown graph command")


def _prepared_result(manifest: GraphImportManifest, path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "graph_schema_version": manifest.graph_schema_version,
        "canonical_schema_version": manifest.canonical_schema_version,
        "canonical_manifest_sha256": manifest.canonical_manifest_sha256,
        "neo4j_version": manifest.neo4j_version,
        "node_counts": manifest.node_counts,
        "relationship_counts": manifest.relationship_counts,
    }


def _dataset_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", type=Path, required=True)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=_json_default))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError(f"cannot serialize value of type {type(value).__name__}")
