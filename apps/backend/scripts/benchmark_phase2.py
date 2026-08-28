from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Self

import duckdb
import polars
import pyarrow
import pyarrow as pa
import pyarrow.parquet as pq

from bitcoin_intel.analytics import AnalyticalDataset, AnalyticalQueries
from bitcoin_intel.analytics.views import register_analytical_views
from bitcoin_intel.benchmarking import SyntheticConfig, write_synthetic_json
from bitcoin_intel.ingestion import ingest_file

_BENCHMARK_VERSION = "1.0"
_WORKLOAD_NAMES = (
    "txid_lookup",
    "address_activity",
    "high_value_ranking",
    "temporal_day",
    "ip_activity",
    "transaction_summary_scan",
)
_LayoutVariant = tuple[str, int, str | None, int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the manual Phase 2 analytical benchmark.")
    parser.add_argument("--records", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duplicate-ratio", type=float, default=0.2)
    parser.add_argument("--min-inputs", type=int, default=1)
    parser.add_argument("--max-inputs", type=int, default=3)
    parser.add_argument("--min-outputs", type=int, default=1)
    parser.add_argument("--max-outputs", type=int, default=3)
    parser.add_argument("--ipv6-ratio", type=float, default=0.25)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--replace-results", action="store_true")
    parser.add_argument("--skip-layout", action="store_true")
    parser.add_argument("--skip-materialization", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    effective_arguments = list(arguments) if arguments is not None else sys.argv[1:]
    if effective_arguments and effective_arguments[0] == "_worker":
        return _worker_main(effective_arguments[1:])

    args = build_parser().parse_args(effective_arguments)
    if any(record_count <= 0 for record_count in args.records):
        raise SystemExit("all --records values must be positive")
    if args.repetitions <= 0:
        raise SystemExit("--repetitions must be positive")

    output_path = args.output.expanduser().resolve(strict=False)
    if output_path.exists() and not args.replace_results:
        raise SystemExit(f"benchmark result already exists: {output_path}")

    if args.work_directory is None:
        with tempfile.TemporaryDirectory(prefix="bitcoin-intel-phase2-") as temporary:
            results = _run_benchmark(args, Path(temporary))
    else:
        work_directory = args.work_directory.expanduser().resolve(strict=False)
        if work_directory.exists():
            raise SystemExit(f"benchmark work directory already exists: {work_directory}")
        work_directory.mkdir(parents=True)
        results = _run_benchmark(args, work_directory)
        if not args.keep_data:
            import shutil

            shutil.rmtree(work_directory)

    _write_results(output_path, results)
    print(f"Benchmark result: {output_path}")
    for ingestion in results["ingestion_scaling"]:
        print(
            "records={record_count} ingestion_seconds={ingestion_seconds:.6f} "
            "records_per_second={records_per_second:.2f} peak_rss_bytes={peak_rss_bytes}".format(
                **ingestion
            )
        )
    return 0


def _run_benchmark(args: argparse.Namespace, work_directory: Path) -> dict[str, Any]:
    ingestion_results: list[dict[str, Any]] = []
    query_results: list[dict[str, Any]] = []
    dataset_paths: dict[int, Path] = {}
    samples: dict[int, dict[str, str]] = {}

    for record_count in args.records:
        run_directory = work_directory / f"records-{record_count}"
        run_directory.mkdir(parents=True)
        worker_result = _run_ingestion_worker(args, record_count, run_directory)
        ingestion_results.append(worker_result)
        dataset_path = run_directory / "dataset"
        dataset_paths[record_count] = dataset_path
        samples[record_count] = {
            "txid": str(worker_result["sample_txid"]),
            "address": str(worker_result["sample_address"]),
            "ip": str(worker_result["sample_ip"]),
        }
        query_results.extend(
            _benchmark_parquet_queries(
                dataset_path,
                record_count=record_count,
                sample=samples[record_count],
                repetitions=args.repetitions,
            )
        )

    largest_count = max(args.records)
    largest_dataset = dataset_paths[largest_count]
    largest_sample = samples[largest_count]
    result: dict[str, Any] = {
        "benchmark_version": _BENCHMARK_VERSION,
        "environment": _environment(),
        "configuration": {
            "record_counts": args.records,
            "seed": args.seed,
            "format": "json",
            "duplicate_observation_ratio": args.duplicate_ratio,
            "input_count_range": [args.min_inputs, args.max_inputs],
            "output_count_range": [args.min_outputs, args.max_outputs],
            "ipv6_ratio": args.ipv6_ratio,
            "query_repetitions": args.repetitions,
            "memory_method": "process RSS sampled every 20 ms; includes generation and ingestion",
            "first_run_label": (
                "first execution in a fresh DuckDB connection; OS cache uncontrolled"
            ),
            "repeated_run_label": "median of subsequent executions in the same connection",
        },
        "ingestion_scaling": ingestion_results,
        "query_performance": query_results,
    }

    if not args.skip_layout:
        result["layout_experiments"] = _benchmark_layouts(
            largest_dataset,
            record_count=largest_count,
            sample_txid=largest_sample["txid"],
            repetitions=args.repetitions,
            output_root=work_directory / "layout-experiments",
        )
        result["query_plans"] = _capture_query_plans(largest_dataset, largest_sample["txid"])
    if not args.skip_materialization:
        result["materialization_experiment"] = _benchmark_materialization(
            largest_dataset,
            record_count=largest_count,
            sample=largest_sample,
            repetitions=args.repetitions,
            database_path=work_directory / "materialized.duckdb",
        )
    return result


def _run_ingestion_worker(
    args: argparse.Namespace, record_count: int, run_directory: Path
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--records",
        str(record_count),
        "--seed",
        str(args.seed),
        "--duplicate-ratio",
        str(args.duplicate_ratio),
        "--min-inputs",
        str(args.min_inputs),
        "--max-inputs",
        str(args.max_inputs),
        "--min-outputs",
        str(args.min_outputs),
        "--max-outputs",
        str(args.max_outputs),
        "--ipv6-ratio",
        str(args.ipv6_ratio),
        "--work-directory",
        str(run_directory),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            f"ingestion worker failed for {record_count} records: {completed.stderr.strip()}"
        )
    return dict(json.loads(completed.stdout))


def _worker_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--records", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--duplicate-ratio", type=float, required=True)
    parser.add_argument("--min-inputs", type=int, required=True)
    parser.add_argument("--max-inputs", type=int, required=True)
    parser.add_argument("--min-outputs", type=int, required=True)
    parser.add_argument("--max-outputs", type=int, required=True)
    parser.add_argument("--ipv6-ratio", type=float, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    args = parser.parse_args(arguments)
    config = SyntheticConfig(
        record_count=args.records,
        seed=args.seed,
        duplicate_observation_ratio=args.duplicate_ratio,
        min_inputs=args.min_inputs,
        max_inputs=args.max_inputs,
        min_outputs=args.min_outputs,
        max_outputs=args.max_outputs,
        ipv6_ratio=args.ipv6_ratio,
    )
    source_path = args.work_directory / "source.json"
    dataset_path = args.work_directory / "dataset"

    with _PeakRssSampler() as sampler:
        generation_started = perf_counter()
        generated = write_synthetic_json(source_path, config)
        generation_seconds = perf_counter() - generation_started
        ingestion_started = perf_counter()
        summary = ingest_file(source_path, dataset_path)
        ingestion_seconds = perf_counter() - ingestion_started

    output_bytes = sum(path.stat().st_size for path in dataset_path.rglob("*") if path.is_file())
    print(
        json.dumps(
            {
                "record_count": summary.records_read,
                "unique_transactions": summary.unique_transactions,
                "accepted": summary.records_accepted,
                "rejected": summary.records_rejected,
                "input_bytes": generated.source_bytes,
                "output_bytes": output_bytes,
                "generation_seconds": generation_seconds,
                "ingestion_seconds": ingestion_seconds,
                "records_per_second": summary.records_read / ingestion_seconds,
                "peak_rss_bytes": sampler.peak_rss_bytes,
                "sample_txid": generated.sample_txid,
                "sample_address": generated.sample_address,
                "sample_ip": generated.sample_ip,
            },
            sort_keys=True,
        )
    )
    return 0


class _PeakRssSampler:
    def __init__(self, interval_seconds: float = 0.02) -> None:
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_rss_bytes = 0

    def __enter__(self) -> Self:
        self._record_sample()
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()
        self._record_sample()

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._record_sample()

    def _record_sample(self) -> None:
        self.peak_rss_bytes = max(self.peak_rss_bytes, _current_rss_bytes())


def _current_rss_bytes() -> int:
    system = platform.system()
    if system == "Windows":
        return _windows_current_rss_bytes()
    statm = Path("/proc/self/statm")
    if statm.is_file():
        resident_pages = int(statm.read_text(encoding="ascii").split()[1])
        posix_os: Any = importlib.import_module("os")
        return resident_pages * int(posix_os.sysconf("SC_PAGE_SIZE"))
    resource = importlib.import_module("resource")
    usage = resource.getrusage(resource.RUSAGE_SELF)
    multiplier = 1 if system == "Darwin" else 1024
    return int(usage.ru_maxrss) * multiplier


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
        ("private_usage", ctypes.c_size_t),
    ]


def _windows_current_rss_bytes() -> int:
    windll = ctypes.windll
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    get_process_memory_info = windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    get_process_memory_info.restype = ctypes.c_int
    process = get_current_process()
    succeeded = get_process_memory_info(process, ctypes.byref(counters), counters.cb)
    if not succeeded:
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.working_set_size)


def _benchmark_parquet_queries(
    dataset_path: Path,
    *,
    record_count: int,
    sample: dict[str, str],
    repetitions: int,
) -> list[dict[str, Any]]:
    dataset = AnalyticalDataset(dataset_path)
    results: list[dict[str, Any]] = []
    for workload_name in _WORKLOAD_NAMES:
        with dataset.connect() as connection:
            result = _measure_workload(
                connection, workload_name, sample=sample, repetitions=repetitions
            )
        results.append({"dataset_records": record_count, "storage": "direct_parquet", **result})
    return results


def _measure_workload(
    connection: duckdb.DuckDBPyConnection,
    workload_name: str,
    *,
    sample: dict[str, str],
    repetitions: int,
) -> dict[str, Any]:
    queries = AnalyticalQueries(connection)
    operation = _workload_operation(connection, queries, workload_name, sample)
    first_started = perf_counter()
    operation()
    first_seconds = perf_counter() - first_started
    repeated_seconds: list[float] = []
    for _ in range(repetitions):
        started = perf_counter()
        operation()
        repeated_seconds.append(perf_counter() - started)
    return {
        "query": workload_name,
        "first_run_ms": first_seconds * 1_000,
        "repeated_median_ms": statistics.median(repeated_seconds) * 1_000,
    }


def _workload_operation(
    connection: duckdb.DuckDBPyConnection,
    queries: AnalyticalQueries,
    workload_name: str,
    sample: dict[str, str],
) -> Callable[[], object]:
    if workload_name == "txid_lookup":
        return lambda: queries.transaction(sample["txid"])
    if workload_name == "address_activity":
        return lambda: queries.address_activity(sample["address"])
    if workload_name == "high_value_ranking":
        return lambda: queries.high_value_transactions(limit=20)
    if workload_name == "temporal_day":
        return lambda: queries.temporal_activity("day")
    if workload_name == "ip_activity":
        return lambda: queries.ip_activity(sample["ip"])
    if workload_name == "transaction_summary_scan":
        return lambda: connection.execute(
            """SELECT count(*), sum(total_input_sats), sum(total_output_sats)
            FROM transaction_summary"""
        ).fetchone()
    raise ValueError(f"unknown benchmark workload: {workload_name}")


def _benchmark_layouts(
    dataset_path: Path,
    *,
    record_count: int,
    sample_txid: str,
    repetitions: int,
    output_root: Path,
) -> dict[str, Any]:
    source_table = pq.read_table(dataset_path / "network_observations" / "part-00000.parquet")
    experiment_groups: dict[str, list[_LayoutVariant]] = {
        "row_group_sizes": [
            ("rows-32768", 32_768, "zstd", 1),
            ("rows-131072", 131_072, "zstd", 1),
            ("rows-524288", 524_288, "zstd", 1),
        ],
        "compression": [
            ("zstd", 65_536, "zstd", 1),
            ("snappy", 65_536, "snappy", 1),
            ("uncompressed", 65_536, None, 1),
        ],
        "file_counts": [
            ("files-1", 65_536, "zstd", 1),
            ("files-4", 65_536, "zstd", 4),
            ("files-32", 65_536, "zstd", 32),
        ],
    }
    result: dict[str, Any] = {
        "dataset_records": record_count,
        "table": "network_observations",
        "table_rows": source_table.num_rows,
    }
    for group_name, variants in experiment_groups.items():
        group_results: list[dict[str, Any]] = []
        for variant_name, row_group_size, compression, file_count in variants:
            variant_directory = output_root / group_name / variant_name
            write_result = _write_layout_variant(
                source_table,
                variant_directory,
                row_group_size=row_group_size,
                compression=compression,
                file_count=file_count,
            )
            query_result = _measure_layout_queries(
                variant_directory,
                sample_txid=sample_txid,
                repetitions=repetitions,
            )
            group_results.append(
                {
                    "variant": variant_name,
                    "row_group_size": row_group_size,
                    "compression": compression or "uncompressed",
                    "requested_file_count": file_count,
                    **write_result,
                    **query_result,
                }
            )
        result[group_name] = group_results
    return result


def _write_layout_variant(
    table: pa.Table,
    directory: Path,
    *,
    row_group_size: int,
    compression: str | None,
    file_count: int,
) -> dict[str, Any]:
    directory.mkdir(parents=True)
    started = perf_counter()
    for file_index in range(file_count):
        start = (table.num_rows * file_index) // file_count
        end = (table.num_rows * (file_index + 1)) // file_count
        if start == end:
            continue
        pq.write_table(
            table.slice(start, end - start),
            directory / f"part-{file_index:05d}.parquet",
            compression=compression,
            use_dictionary=True,
            write_statistics=True,
            row_group_size=row_group_size,
            version="2.6",
        )
    write_seconds = perf_counter() - started
    files = sorted(directory.glob("*.parquet"))
    return {
        "actual_file_count": len(files),
        "row_group_count": sum(pq.ParquetFile(path).num_row_groups for path in files),
        "write_ms": write_seconds * 1_000,
        "bytes": sum(path.stat().st_size for path in files),
    }


def _measure_layout_queries(
    directory: Path, *, sample_txid: str, repetitions: int
) -> dict[str, Any]:
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.read_parquet(str(directory / "*.parquet")).create_view("observations")
        time_start = datetime(2026, 1, 15, tzinfo=UTC)
        time_end = time_start + timedelta(days=1)
        operations: dict[str, Callable[[], object]] = {
            "full_scan": lambda: connection.execute(
                """SELECT count(*), count(DISTINCT txid), min(observed_at), max(observed_at)
                FROM observations"""
            ).fetchone(),
            "selective_txid": lambda: connection.execute(
                """SELECT observation_id, observed_at FROM observations
                WHERE txid = ? ORDER BY observed_at""",
                [sample_txid],
            ).fetchall(),
            "time_range": lambda: connection.execute(
                """SELECT count(*), count(DISTINCT txid) FROM observations
                WHERE observed_at >= ? AND observed_at < ?""",
                [time_start, time_end],
            ).fetchone(),
        }
        result: dict[str, Any] = {}
        for name, operation in operations.items():
            first_started = perf_counter()
            operation()
            result[f"{name}_first_ms"] = (perf_counter() - first_started) * 1_000
            repeated: list[float] = []
            for _ in range(repetitions):
                started = perf_counter()
                operation()
                repeated.append((perf_counter() - started) * 1_000)
            result[f"{name}_repeated_median_ms"] = statistics.median(repeated)
        return result
    finally:
        connection.close()


def _capture_query_plans(dataset_path: Path, sample_txid: str) -> dict[str, Any]:
    dataset = AnalyticalDataset(dataset_path)
    with dataset.connect() as connection:
        txid_plan_row = connection.execute(
            "EXPLAIN SELECT txid, fee_sats FROM transactions WHERE txid = ?",
            [sample_txid],
        ).fetchone()
        time_plan_row = connection.execute(
            """EXPLAIN SELECT observation_id FROM network_observations
            WHERE observed_at >= ? AND observed_at < ?""",
            [
                datetime(2026, 1, 15, tzinfo=UTC),
                datetime(2026, 1, 16, tzinfo=UTC),
            ],
        ).fetchone()
        if txid_plan_row is None or time_plan_row is None:
            raise AssertionError("DuckDB EXPLAIN unexpectedly returned no plan")
        txid_plan = str(txid_plan_row[1])
        time_plan = str(time_plan_row[1])
    return {
        "txid_filter": txid_plan,
        "time_filter": time_plan,
        "filter_visible_in_txid_plan": "Filters:" in txid_plan,
        "filter_visible_in_time_plan": "Filters:" in time_plan,
        "projection_visible_in_txid_plan": "Projections:" in txid_plan,
        "projection_visible_in_time_plan": "Projections:" in time_plan,
    }


def _benchmark_materialization(
    dataset_path: Path,
    *,
    record_count: int,
    sample: dict[str, str],
    repetitions: int,
    database_path: Path,
) -> dict[str, Any]:
    dataset = AnalyticalDataset(dataset_path)
    build_started = perf_counter()
    connection = duckdb.connect(str(database_path))
    try:
        connection.execute("SET TimeZone = 'UTC'")
        for table_name, table in dataset.manifest.tables.items():
            connection.read_parquet(str(table.path)).create(table_name)
        register_analytical_views(connection)
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    build_seconds = perf_counter() - build_started

    query_results: list[dict[str, Any]] = []
    for workload_name in _WORKLOAD_NAMES:
        connection = duckdb.connect(str(database_path), read_only=True)
        try:
            connection.execute("SET TimeZone = 'UTC'")
            result = _measure_workload(
                connection, workload_name, sample=sample, repetitions=repetitions
            )
        finally:
            connection.close()
        query_results.append(result)
    return {
        "dataset_records": record_count,
        "build_seconds": build_seconds,
        "database_bytes": database_path.stat().st_size,
        "indexes_created": 0,
        "queries": query_results,
    }


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or "not reported",
        "logical_cpu_count": os.cpu_count(),
        "total_memory_bytes": _total_memory_bytes(),
        "python_version": platform.python_version(),
        "duckdb_version": duckdb.__version__,
        "polars_version": polars.__version__,
        "pyarrow_version": pyarrow.__version__,
    }


def _total_memory_bytes() -> int | None:
    if platform.system() == "Windows":

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.length = ctypes.sizeof(status)
        windll = ctypes.windll
        if windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
        return None
    try:
        posix_os: Any = importlib.import_module("os")
        pages = int(posix_os.sysconf("SC_PHYS_PAGES"))
        page_size = int(posix_os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        return None
    return pages * page_size


def _write_results(output_path: Path, results: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    temporary_path.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_path.replace(output_path)


if __name__ == "__main__":
    raise SystemExit(main())
