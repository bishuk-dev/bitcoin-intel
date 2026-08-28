from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

import duckdb
import igraph
import pyarrow

from bitcoin_intel.benchmarking import SyntheticConfig, write_synthetic_json
from bitcoin_intel.features import build_features
from bitcoin_intel.ingestion import ingest_file

_BENCHMARK_VERSION = "1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the manual Phase 4 feature benchmark.")
    parser.add_argument("--records", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--replace-results", action="store_true")
    parser.add_argument(
        "--reuse-canonical",
        action="store_true",
        help="reuse canonical datasets already present under the work directory",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    effective = list(arguments) if arguments is not None else sys.argv[1:]
    if effective and effective[0] == "_worker":
        return _worker_main(effective[1:])
    args = build_parser().parse_args(effective)
    if any(count <= 0 for count in args.records):
        raise SystemExit("all --records values must be positive")
    output = args.output.expanduser().resolve(strict=False)
    if output.exists() and not args.replace_results:
        raise SystemExit(f"benchmark result already exists: {output}")

    if args.work_directory is None:
        with tempfile.TemporaryDirectory(prefix="bitcoin-intel-phase4-") as temporary:
            results = _run(args, Path(temporary))
    else:
        work = args.work_directory.expanduser().resolve(strict=False)
        if work.exists() and not args.reuse_canonical:
            raise SystemExit(f"benchmark work directory already exists: {work}")
        work.mkdir(parents=True, exist_ok=args.reuse_canonical)
        results = _run(args, work)
        if not args.keep_data and not args.reuse_canonical:
            import shutil

            shutil.rmtree(work)
    _write_json(output, results)
    print(f"Benchmark result: {output}")
    for result in results["feature_builds"]:
        print(
            "records={record_count} seconds={build_seconds:.6f} "
            "peak_rss_bytes={peak_rss_bytes} output_bytes={output_bytes}".format(**result)
        )
    return 0


def _run(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    builds: list[dict[str, Any]] = []
    for count in args.records:
        run = work / f"records-{count}"
        run.mkdir(parents=True, exist_ok=args.reuse_canonical)
        source = run / "source.json"
        dataset = run / "dataset"
        canonical_preparation: dict[str, object]
        if args.reuse_canonical and (dataset / "manifest.json").is_file():
            canonical_preparation = {
                "reused_existing_canonical_dataset": True,
                "excluded_from_feature_timing_and_peak_rss": True,
            }
        else:
            generation = write_synthetic_json(
                source, SyntheticConfig(record_count=count, seed=args.seed)
            )
            ingestion = ingest_file(source, dataset)
            canonical_preparation = {
                "generated_records": generation.record_count,
                "accepted_records": ingestion.records_accepted,
                "unique_transactions": ingestion.unique_transactions,
                "reused_existing_canonical_dataset": False,
                "excluded_from_feature_timing_and_peak_rss": True,
            }
        builds.append(_run_worker(dataset, run / "features", count))
        builds[-1]["canonical_preparation"] = canonical_preparation
    return {
        "benchmark_version": _BENCHMARK_VERSION,
        "environment": _environment(),
        "configuration": {
            "record_counts": args.records,
            "seed": args.seed,
            "temporal_mode": "snapshot",
            "memory_method": "feature worker process RSS sampled every 20 ms",
            "canonical_preparation": "generated once per size; excluded from feature measurements",
        },
        "feature_builds": builds,
    }


def _run_worker(dataset: Path, output: Path, record_count: int) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--dataset",
        str(dataset),
        "--output",
        str(output),
        "--records",
        str(record_count),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"feature worker failed for {record_count}: {completed.stderr.strip()}")
    return dict(json.loads(completed.stdout))


def _worker_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records", type=int, required=True)
    args = parser.parse_args(arguments)
    sampler = _RssSampler()
    sampler.start()
    started = perf_counter()
    try:
        summary = build_features(args.dataset, args.output)
    finally:
        sampler.stop()
    elapsed = perf_counter() - started
    output_bytes = sum(path.stat().st_size for path in args.output.rglob("*") if path.is_file())
    result = {
        "record_count": args.records,
        "build_seconds": elapsed,
        "peak_rss_bytes": sampler.peak_rss_bytes,
        "output_bytes": output_bytes,
        "feature_rows_total": sum(summary.table_rows.values()),
        "table_rows": summary.table_rows,
        "feature_dataset_id": summary.feature_dataset_id,
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


class _RssSampler:
    def __init__(self) -> None:
        self.peak_rss_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def start(self) -> None:
        self.peak_rss_bytes = _current_rss_bytes()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _sample(self) -> None:
        while not self._stop.wait(0.02):
            self.peak_rss_bytes = max(self.peak_rss_bytes, _current_rss_bytes())
        self.peak_rss_bytes = max(self.peak_rss_bytes, _current_rss_bytes())


def _current_rss_bytes() -> int:
    if platform.system() == "Windows":
        return _windows_current_rss_bytes()
    resource: Any = importlib.import_module("resource")
    usage = resource.getrusage(resource.RUSAGE_SELF)
    multiplier = 1 if platform.system() == "Darwin" else 1024
    return int(usage.ru_maxrss) * multiplier


def _windows_current_rss_bytes() -> int:
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

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    windll = ctypes.windll
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
    if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.working_set_size)


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or "not reported",
        "logical_cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "duckdb_version": duckdb.__version__,
        "pyarrow_version": pyarrow.__version__,
        "igraph_version": igraph.__version__,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
