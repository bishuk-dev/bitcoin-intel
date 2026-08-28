from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.graph.config import GraphSettings
from bitcoin_intel.graph.connection import GraphConnection, GraphConnectionError
from bitcoin_intel.graph.constants import GRAPH_DATABASE_NAME
from bitcoin_intel.graph.import_builder import prepare_graph_import
from bitcoin_intel.graph.models import GraphIntegrityReport, PreparedGraphImport
from bitcoin_intel.graph.schema import ensure_graph_constraints
from bitcoin_intel.graph.validation import validate_graph

_LOGGER = logging.getLogger(__name__)


class GraphRebuildError(RuntimeError):
    """Raised when the explicit Docker-based graph rebuild cannot complete."""


@dataclass(frozen=True, slots=True)
class GraphRebuildResult:
    prepared: PreparedGraphImport
    dry_run_seconds: float
    import_seconds: float
    integrity: GraphIntegrityReport


def neo4j_admin_import_arguments(
    *,
    import_root: str = "/import",
    database: str = GRAPH_DATABASE_NAME,
    dry_run: bool = False,
    max_off_heap_memory: str = "1G",
    threads: int = 2,
) -> tuple[str, ...]:
    root = import_root.rstrip("/")
    arguments = [
        "neo4j-admin",
        "database",
        "import",
        "full",
        "--input-type=parquet",
        "--path-pattern-style=none",
        "--id-type=string",
        "--normalize-types=true",
        "--strict=true",
        "--skip-duplicate-nodes=false",
        "--skip-bad-relationships=false",
        "--bad-tolerance=0",
        "--overwrite-destination=true",
        f"--max-off-heap-memory={max_off_heap_memory}",
        f"--threads={threads}",
        # Put the positional database before variadic file options so the final
        # --relationships option cannot consume it as another input path.
        database,
        (
            f"--nodes=Transaction={root}/headers/transaction_nodes.header.csv,"
            f"{root}/transaction_nodes.parquet"
        ),
        (f"--nodes=Address={root}/headers/address_nodes.header.csv,{root}/address_nodes.parquet"),
        (
            f"--nodes=IPAddress={root}/headers/ip_address_nodes.header.csv,"
            f"{root}/ip_address_nodes.parquet"
        ),
        (
            f"--nodes=NetworkObservation="
            f"{root}/headers/network_observation_nodes.header.csv,"
            f"{root}/network_observation_nodes.parquet"
        ),
        (
            f"--relationships=SPENT_IN={root}/headers/spent_in_relationships.header.csv,"
            f"{root}/spent_in_relationships.parquet"
        ),
        (
            f"--relationships=CREATED_OUTPUT="
            f"{root}/headers/created_output_relationships.header.csv,"
            f"{root}/created_output_relationships.parquet"
        ),
        (
            f"--relationships=OBSERVED_TRANSACTION="
            f"{root}/headers/observed_transaction_relationships.header.csv,"
            f"{root}/observed_transaction_relationships.parquet"
        ),
        (
            f"--relationships=SOURCE_IP={root}/headers/source_ip_relationships.header.csv,"
            f"{root}/source_ip_relationships.parquet"
        ),
        (
            f"--relationships=DESTINATION_IP="
            f"{root}/headers/destination_ip_relationships.header.csv,"
            f"{root}/destination_ip_relationships.parquet"
        ),
    ]
    if dry_run:
        arguments.append("--dry-run=true")
    return tuple(arguments)


def rebuild_graph(
    *,
    dataset_path: Path,
    output_path: Path,
    compose_file: Path,
    settings: GraphSettings,
    confirm_replace_database: bool,
    compose_project_name: str | None = None,
    max_off_heap_memory: str = "1G",
    import_threads: int = 2,
    startup_timeout_seconds: float = 120.0,
) -> GraphRebuildResult:
    if not confirm_replace_database:
        raise GraphRebuildError(
            "graph rebuild replaces the Neo4j database; pass --confirm-replace-database"
        )
    if not max_off_heap_memory or any(character.isspace() for character in max_off_heap_memory):
        raise ValueError("import max off-heap memory must be a non-empty Neo4j size value")
    if isinstance(import_threads, bool) or not 1 <= import_threads <= 32:
        raise ValueError("import threads must be an integer from 1 through 32")
    if compose_project_name is not None and not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]*", compose_project_name
    ):
        raise ValueError(
            "Compose project name must start with a lowercase letter or digit and contain only "
            "lowercase letters, digits, underscores, or hyphens"
        )
    resolved_compose = compose_file.expanduser().resolve(strict=True)
    prepared = prepare_graph_import(dataset_path, output_path)
    environment = _compose_environment(settings)
    if compose_project_name is not None:
        environment["COMPOSE_PROJECT_NAME"] = compose_project_name
    compose = ("docker", "compose", "--file", str(resolved_compose))
    volume = f"{prepared.path}:/import:ro"

    dry_started = time.perf_counter()
    _LOGGER.info("Neo4j import dry run started: database=%s", settings.neo4j_database)
    _run(
        (
            *compose,
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "neo4j",
            *neo4j_admin_import_arguments(
                dry_run=True,
                max_off_heap_memory=max_off_heap_memory,
                threads=import_threads,
            ),
        ),
        cwd=resolved_compose.parent,
        environment=environment,
        action="Neo4j import dry run",
    )
    dry_seconds = time.perf_counter() - dry_started
    _LOGGER.info("Neo4j import dry run completed: seconds=%.3f", dry_seconds)

    _run(
        (*compose, "stop", "neo4j"),
        cwd=resolved_compose.parent,
        environment=environment,
        action="stop Neo4j before replacement",
    )
    import_started = time.perf_counter()
    _LOGGER.info("Neo4j database import started: database=%s", settings.neo4j_database)
    _run(
        (
            *compose,
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            volume,
            "neo4j",
            *neo4j_admin_import_arguments(
                max_off_heap_memory=max_off_heap_memory,
                threads=import_threads,
            ),
        ),
        cwd=resolved_compose.parent,
        environment=environment,
        action="strict Neo4j full import",
    )
    import_seconds = time.perf_counter() - import_started
    _LOGGER.info("Neo4j database import completed: seconds=%.3f", import_seconds)
    _run(
        (*compose, "up", "--detach", "neo4j"),
        cwd=resolved_compose.parent,
        environment=environment,
        action="start imported Neo4j database",
    )
    _wait_for_graph(settings, startup_timeout_seconds)
    canonical_dataset = AnalyticalDataset(dataset_path)
    connection = GraphConnection(settings)
    with connection.connect() as driver:
        names = ensure_graph_constraints(driver, settings.neo4j_database)
        if len(names) != 4:
            raise GraphRebuildError("Neo4j did not create all four factual identity constraints")
        integrity = validate_graph(driver, settings.neo4j_database, canonical_dataset)
    if not integrity.is_valid:
        codes = ", ".join(issue.code for issue in integrity.issues)
        raise GraphRebuildError(f"imported graph failed canonical validation: {codes}")
    _LOGGER.info(
        "graph validation completed: database=%s nodes=%d relationships=%d",
        settings.neo4j_database,
        (
            integrity.graph_counts.transactions
            + integrity.graph_counts.addresses
            + integrity.graph_counts.ip_addresses
            + integrity.graph_counts.network_observations
        ),
        (
            integrity.graph_counts.spent_in
            + integrity.graph_counts.created_output
            + integrity.graph_counts.observed_transaction
            + integrity.graph_counts.source_ip
            + integrity.graph_counts.destination_ip
        ),
    )
    return GraphRebuildResult(prepared, dry_seconds, import_seconds, integrity)


def _compose_environment(settings: GraphSettings) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "NEO4J_URI": settings.neo4j_uri,
            "NEO4J_USER": settings.neo4j_user,
            "NEO4J_PASSWORD": settings.neo4j_password.get_secret_value(),
            "NEO4J_DATABASE": settings.neo4j_database,
        }
    )
    return environment


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    action: str,
) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GraphRebuildError(f"failed to {action}: {detail}")


def _wait_for_graph(settings: GraphSettings, timeout_seconds: float) -> None:
    readiness_settings = settings.model_copy(
        update={"neo4j_uri": _direct_bolt_uri(settings.neo4j_uri)}
    )
    deadline = time.monotonic() + timeout_seconds
    last_error: GraphConnectionError | None = None
    while time.monotonic() < deadline:
        try:
            with GraphConnection(readiness_settings).connect():
                _LOGGER.info("Neo4j database is ready: database=%s", settings.neo4j_database)
                return
        except GraphConnectionError as error:
            last_error = error
            time.sleep(1)
    raise GraphRebuildError(f"Neo4j did not become ready: {last_error}")


def _direct_bolt_uri(uri: str) -> str:
    schemes = {
        "neo4j://": "bolt://",
        "neo4j+s://": "bolt+s://",
        "neo4j+ssc://": "bolt+ssc://",
    }
    for source, target in schemes.items():
        if uri.startswith(source):
            return target + uri[len(source) :]
    return uri
