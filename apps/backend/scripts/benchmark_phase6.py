from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

import duckdb
import maxminddb
import pyarrow
import pyarrow.parquet as pq
from mmdb_writer import MMDBWriter
from netaddr import IPSet

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.benchmarking import SyntheticConfig, write_synthetic_json
from bitcoin_intel.enrichment import build_ip_enrichment
from bitcoin_intel.features import build_features
from bitcoin_intel.ingestion import ingest_file

_BENCHMARK_VERSION = "1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the manual Phase 6 benchmark.")
    parser.add_argument("--records", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--replace-results", action="store_true")
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
        with tempfile.TemporaryDirectory(prefix="bitcoin-intel-phase6-") as temporary:
            results = _run(args, Path(temporary))
    else:
        work = args.work_directory.expanduser().resolve(strict=False)
        if work.exists():
            raise SystemExit(f"benchmark work directory already exists: {work}")
        work.mkdir(parents=True)
        results = _run(args, work)
        if not args.keep_data:
            shutil.rmtree(work)
    _write_json(output, results)
    print(f"Benchmark result: {output}")
    for result in results["builds"]:
        enrichment = result["enrichment"]
        features = result["feature_v2"]
        print(
            f"records={result['record_count']} distinct_ips={result['distinct_ips']} "
            f"enrichment_seconds={enrichment['build_seconds']:.6f} "
            f"lookups_per_second={enrichment['lookups_per_second']:.2f} "
            f"feature_seconds={features['build_seconds']:.6f}"
        )
    return 0


def _run(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    country, asn = _write_benchmark_mmdbs(work / "resources")
    builds: list[dict[str, Any]] = []
    for count in args.records:
        run = work / f"records-{count}"
        run.mkdir()
        source, dataset = run / "source.json", run / "dataset"
        generation = write_synthetic_json(
            source, SyntheticConfig(record_count=count, seed=args.seed)
        )
        ingestion = ingest_file(source, dataset)
        with AnalyticalDataset(dataset).connect() as connection:
            distinct_ip_row = connection.execute(
                """SELECT count(*) FROM (
                SELECT src_ip AS ip FROM network_observations
                UNION SELECT dst_ip AS ip FROM network_observations)"""
            ).fetchone()
            if distinct_ip_row is None:
                raise AssertionError("distinct-IP benchmark query returned no row")
            distinct_ips = int(distinct_ip_row[0])
        enrichment_output = run / "enrichment"
        enrichment_result = _run_worker(
            "enrichment", dataset, enrichment_output, country, asn, count
        )
        feature_result = _run_worker(
            "features", dataset, run / "features-v2", country, asn, count, enrichment_output
        )
        builds.append(
            {
                "record_count": count,
                "canonical_observations": ingestion.network_observations,
                "distinct_ips": distinct_ips,
                "canonical_preparation": {
                    "generated_records": generation.record_count,
                    "accepted_records": ingestion.records_accepted,
                    "unique_transactions": ingestion.unique_transactions,
                    "excluded_from_measured_workers": True,
                },
                "enrichment": enrichment_result,
                "feature_v2": feature_result,
            }
        )
    return {
        "benchmark_version": _BENCHMARK_VERSION,
        "environment": _environment(),
        "configuration": {
            "record_counts": args.records,
            "seed": args.seed,
            "temporal_mode": "snapshot",
            "mmdb_fixture": "purpose-built deterministic-shape benchmark resources",
            "memory_method": "fresh worker RSS sampled every 20 ms",
            "lookup_count_definition": "country plus ASN lookup for every distinct IP",
            "scope": "enrichment and Feature Schema v2 only",
        },
        "builds": builds,
    }


def _run_worker(
    mode: str,
    dataset: Path,
    output: Path,
    country: Path,
    asn: Path,
    record_count: int,
    enrichment: Path | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--mode",
        mode,
        "--dataset",
        str(dataset),
        "--output",
        str(output),
        "--country-db",
        str(country),
        "--asn-db",
        str(asn),
        "--records",
        str(record_count),
    ]
    if enrichment is not None:
        command.extend(["--enrichment", str(enrichment)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"{mode} worker failed for {record_count}: {completed.stderr.strip()}")
    return dict(json.loads(completed.stdout))


def _worker_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=("enrichment", "features"), required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--country-db", type=Path, required=True)
    parser.add_argument("--asn-db", type=Path, required=True)
    parser.add_argument("--enrichment", type=Path)
    parser.add_argument("--records", type=int, required=True)
    args = parser.parse_args(arguments)
    sampler = _RssSampler()
    sampler.start()
    started = perf_counter()
    summary: Any
    try:
        if args.mode == "enrichment":
            summary = build_ip_enrichment(args.dataset, args.output, args.country_db, args.asn_db)
        else:
            if args.enrichment is None:
                raise ValueError("feature worker requires --enrichment")
            summary = build_features(args.dataset, args.output, args.enrichment)
    finally:
        sampler.stop()
    elapsed = perf_counter() - started
    output_bytes = sum(path.stat().st_size for path in args.output.rglob("*") if path.is_file())
    if args.mode == "enrichment":
        table = pq.read_table(args.output / "ip_enrichment" / "part-00000.parquet")
        distinct_ips = table.num_rows
        country_matches = sum(table["country_found"].to_pylist())
        asn_matches = sum(table["asn_found"].to_pylist())
        ipv4 = sum("." in value for value in table["ip"].to_pylist())
        total_lookups = distinct_ips * 2
        result = {
            "build_seconds": elapsed,
            "peak_rss_bytes": sampler.peak_rss_bytes,
            "output_bytes": output_bytes,
            "total_lookups": total_lookups,
            "lookups_per_second": total_lookups / elapsed,
            "country_matches": country_matches,
            "country_misses": distinct_ips - country_matches,
            "asn_matches": asn_matches,
            "asn_misses": distinct_ips - asn_matches,
            "ipv4": ipv4,
            "ipv6": distinct_ips - ipv4,
            "enrichment_dataset_id": summary.enrichment_dataset_id,
        }
    else:
        result = {
            "build_seconds": elapsed,
            "peak_rss_bytes": sampler.peak_rss_bytes,
            "output_bytes": output_bytes,
            "feature_rows_total": sum(summary.table_rows.values()),
            "table_rows": summary.table_rows,
            "feature_dataset_id": summary.feature_dataset_id,
        }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


def _write_benchmark_mmdbs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    country, asn = root / "country.mmdb", root / "asn.mmdb"
    networks = (
        ("192.0.2.0/25", "IN", 64500, "Benchmark IN"),
        ("198.51.100.0/25", "US", 64501, "Benchmark US"),
        ("2001:db8::/113", "DE", 64502, "Benchmark DE"),
        ("2001:db8:1::/113", "SG", 64503, "Benchmark SG"),
    )
    country_writer = MMDBWriter(
        ip_version=6, ipv4_compatible=True, database_type="DBIP-Country-Lite"
    )
    asn_writer = MMDBWriter(ip_version=6, ipv4_compatible=True, database_type="DBIP-ASN-Lite")
    for network, code, number, organization in networks:
        country_writer.insert_network(IPSet([network]), {"country": {"iso_code": code}})
        asn_writer.insert_network(
            IPSet([network]),
            {
                "autonomous_system_number": number,
                "autonomous_system_organization": organization,
            },
        )
    country_writer.to_db_file(str(country))
    asn_writer.to_db_file(str(asn))
    return country, asn


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
        "maxminddb_version": maxminddb.__version__,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
