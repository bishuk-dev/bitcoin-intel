from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Self

import neo4j

from bitcoin_intel.benchmarking import SyntheticConfig, write_synthetic_json
from bitcoin_intel.graph.config import GraphSettings
from bitcoin_intel.graph.connection import GraphConnection
from bitcoin_intel.graph.docker import rebuild_graph
from bitcoin_intel.graph.gds import verify_gds_foundation
from bitcoin_intel.graph.import_builder import prepare_graph_import
from bitcoin_intel.graph.models import GraphNodeIdentity
from bitcoin_intel.graph.queries import GraphQueries
from bitcoin_intel.graph.schema import plugin_versions
from bitcoin_intel.ingestion import ingest_file

_BENCHMARK_VERSION = "1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the manual Phase 3 graph benchmark")
    parser.add_argument("--records", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, default=Path("../../docker-compose.yml"))
    parser.add_argument("--compose-project-name", default="sih26146-phase3-verification")
    parser.add_argument("--replace-results", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if any(value <= 0 for value in args.records):
        raise SystemExit("all --records values must be positive")
    if args.repetitions <= 0:
        raise SystemExit("--repetitions must be positive")
    work_directory = args.work_directory.expanduser().resolve(strict=False)
    if work_directory.exists():
        raise SystemExit(f"benchmark work directory already exists: {work_directory}")
    output = args.output.expanduser().resolve(strict=False)
    if output.exists() and not args.replace_results:
        raise SystemExit(f"benchmark result already exists: {output}")
    work_directory.mkdir(parents=True)

    settings = GraphSettings()  # type: ignore[call-arg]
    compose_file = args.compose_file.expanduser().resolve(strict=True)
    datasets: list[dict[str, Any]] = []
    for record_count in args.records:
        datasets.append(
            _benchmark_dataset(
                record_count=record_count,
                seed=args.seed,
                repetitions=args.repetitions,
                root=work_directory / f"records-{record_count}",
                compose_file=compose_file,
                compose_project_name=args.compose_project_name,
                settings=settings,
            )
        )

    result = {
        "benchmark_version": _BENCHMARK_VERSION,
        "measured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor() or "not reported",
            "logical_cpu_count": os.cpu_count(),
            "python_version": platform.python_version(),
            "neo4j_driver_version": neo4j.__version__,
        },
        "configuration": {
            "record_counts": args.records,
            "seed": args.seed,
            "duplicate_observation_ratio": 0.2,
            "input_count_range": [1, 3],
            "output_count_range": [1, 3],
            "ipv6_ratio": 0.25,
            "query_repetitions": args.repetitions,
            "import_threads": 2,
            "max_off_heap_memory": "1G",
            "first_run_label": "first call after opening one verified driver",
            "repeated_run_label": "median of subsequent calls through the same driver",
            "memory_method": (
                "maximum Docker-reported container memory sampled every 500 ms across the "
                "isolated Compose project during rebuild"
            ),
        },
        "datasets": datasets,
    }
    _write_json(output, result)
    print(output)
    return 0


def _benchmark_dataset(
    *,
    record_count: int,
    seed: int,
    repetitions: int,
    root: Path,
    compose_file: Path,
    compose_project_name: str,
    settings: GraphSettings,
) -> dict[str, Any]:
    root.mkdir()
    source_path = root / "source.json"
    dataset_path = root / "dataset"
    generated = write_synthetic_json(
        source_path,
        SyntheticConfig(
            record_count=record_count,
            seed=seed,
            duplicate_observation_ratio=0.2,
            min_inputs=1,
            max_inputs=3,
            min_outputs=1,
            max_outputs=3,
            ipv6_ratio=0.25,
        ),
    )
    ingestion = ingest_file(source_path, dataset_path)

    preparation_started = perf_counter()
    prepared = prepare_graph_import(dataset_path, root / "prepared-import")
    preparation_seconds = perf_counter() - preparation_started
    graph_import_bytes = sum(file.bytes for file in prepared.manifest.files.values())

    with _DockerMemorySampler(compose_project_name) as memory:
        rebuilt = rebuild_graph(
            dataset_path=dataset_path,
            output_path=root / "rebuild-import",
            compose_file=compose_file,
            settings=settings,
            confirm_replace_database=True,
            compose_project_name=compose_project_name,
            startup_timeout_seconds=180,
        )

    with GraphConnection(settings).connect() as driver:
        versions = plugin_versions(driver, settings.neo4j_database)
        query_results = _benchmark_queries(
            GraphQueries(driver, settings.neo4j_database),
            sample_txid=generated.sample_txid,
            sample_address=generated.sample_address,
            sample_ip=generated.sample_ip,
            repetitions=repetitions,
        )
        gds = _measure(
            lambda: verify_gds_foundation(driver, settings.neo4j_database), repetitions=1
        )
        gds_result = gds.pop("last_result")
        if not hasattr(gds_result, "component_count"):
            raise AssertionError("GDS benchmark did not return its verification model")

    store_bytes = _neo4j_store_bytes(
        compose_file=compose_file,
        compose_project_name=compose_project_name,
        settings=settings,
    )
    counts = rebuilt.integrity.graph_counts
    total_nodes = (
        counts.transactions + counts.addresses + counts.ip_addresses + counts.network_observations
    )
    total_relationships = (
        counts.spent_in
        + counts.created_output
        + counts.observed_transaction
        + counts.source_ip
        + counts.destination_ip
    )
    return {
        "canonical_records": record_count,
        "unique_transactions": ingestion.unique_transactions,
        "unique_addresses": counts.addresses,
        "unique_ips": counts.ip_addresses,
        "observations": counts.network_observations,
        "node_count": total_nodes,
        "relationship_count": total_relationships,
        "counts": asdict(counts),
        "graph_import_preparation_seconds": preparation_seconds,
        "graph_import_parquet_bytes": graph_import_bytes,
        "neo4j_admin_dry_run_seconds": rebuilt.dry_run_seconds,
        "neo4j_admin_import_seconds": rebuilt.import_seconds,
        "peak_container_memory_bytes": memory.peak_bytes,
        "neo4j_store_bytes": store_bytes,
        "versions": asdict(versions),
        "query_performance": query_results,
        "gds_performance": {
            "first_run_ms": gds["first_run_ms"],
            "repeated_median_ms": gds["repeated_median_ms"],
            **asdict(gds_result),
        },
    }


def _benchmark_queries(
    queries: GraphQueries,
    *,
    sample_txid: str,
    sample_address: str,
    sample_ip: str,
    repetitions: int,
) -> list[dict[str, Any]]:
    workloads: dict[str, Callable[[], object]] = {
        "txid_neighborhood": lambda: queries.transaction_neighborhood(sample_txid),
        "address_transactions": lambda: queries.address_transactions(sample_address),
        "ip_observations": lambda: queries.ip_observations(sample_ip),
        "bounded_path": lambda: queries.shortest_path(
            GraphNodeIdentity("address", sample_address),
            GraphNodeIdentity("transaction", sample_txid),
            max_depth=2,
        ),
    }
    return [
        {"query": name, **_without_result(_measure(operation, repetitions=repetitions))}
        for name, operation in workloads.items()
    ]


def _measure(operation: Callable[[], object], *, repetitions: int) -> dict[str, Any]:
    started = perf_counter()
    last_result = operation()
    first_ms = (perf_counter() - started) * 1_000
    repeated: list[float] = []
    for _ in range(repetitions):
        started = perf_counter()
        last_result = operation()
        repeated.append((perf_counter() - started) * 1_000)
    return {
        "first_run_ms": first_ms,
        "repeated_median_ms": statistics.median(repeated),
        "last_result": last_result,
    }


def _without_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "last_result"}


class _DockerMemorySampler:
    def __init__(self, project_name: str) -> None:
        self._project_name = project_name
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_bytes: int | None = None

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(0.5):
            self._sample()

    def _sample(self) -> None:
        listed = subprocess.run(
            (
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={self._project_name}",
                "--format",
                "{{.ID}}",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if listed.returncode:
            return
        for container_id in listed.stdout.split():
            stats = subprocess.run(
                (
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.MemUsage}}",
                    container_id,
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            if stats.returncode or not stats.stdout.strip():
                continue
            try:
                current = _memory_bytes(stats.stdout.split("/", maxsplit=1)[0].strip())
            except ValueError:
                continue
            self.peak_bytes = current if self.peak_bytes is None else max(self.peak_bytes, current)


def _memory_bytes(value: str) -> int:
    units = {
        "B": 1,
        "kB": 1_000,
        "KiB": 1_024,
        "MB": 1_000**2,
        "MiB": 1_024**2,
        "GB": 1_000**3,
        "GiB": 1_024**3,
    }
    for unit in sorted(units, key=len, reverse=True):
        if value.endswith(unit):
            return int(float(value[: -len(unit)].strip()) * units[unit])
    raise ValueError(f"unsupported Docker memory value: {value}")


def _neo4j_store_bytes(
    *, compose_file: Path, compose_project_name: str, settings: GraphSettings
) -> int:
    environment = dict(os.environ)
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": compose_project_name,
            "NEO4J_USER": settings.neo4j_user,
            "NEO4J_PASSWORD": settings.neo4j_password.get_secret_value(),
        }
    )
    completed = subprocess.run(
        (
            "docker",
            "compose",
            "--file",
            str(compose_file),
            "exec",
            "--no-TTY",
            "neo4j",
            "du",
            "-sb",
            "/data/databases/neo4j",
        ),
        env=environment,
        cwd=compose_file.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(f"failed to measure Neo4j store: {completed.stderr.strip()}")
    try:
        return int(completed.stdout.split()[0])
    except (IndexError, ValueError) as error:
        raise RuntimeError(f"unexpected Neo4j store measurement: {completed.stdout!r}") from error


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
