from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import joblib
import numpy
import pyarrow
import sklearn

from bitcoin_intel.benchmarking import ScenarioConfig, write_scenario_bundle
from bitcoin_intel.features import build_features_v1
from bitcoin_intel.ingestion import ingest_file
from bitcoin_intel.ml import ExperimentConfig, run_experiment

_BENCHMARK_VERSION = "1.0"
_EXPERIMENTS = (
    ("anomaly", "isolation-forest", "all-eligible"),
    ("anomaly", "local-outlier-factor", "all-eligible"),
    ("scenario", "logistic-regression", "all-eligible"),
    ("scenario", "random-forest", "all-eligible"),
    ("scenario", "logistic-regression", "transaction-only"),
    ("scenario", "logistic-regression", "network-only"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the manual Phase 5 ML baseline benchmark.")
    parser.add_argument("--transactions", type=int, nargs="+", default=[10_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--group-size", type=int, default=20)
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
    if any(count < 100 for count in args.transactions):
        raise SystemExit("all --transactions values must be at least 100")
    output = args.output.expanduser().resolve(strict=False)
    if output.exists() and not args.replace_results:
        raise SystemExit(f"benchmark result already exists: {output}")
    if args.work_directory is None:
        with tempfile.TemporaryDirectory(prefix="bitcoin-intel-phase5-") as temporary:
            result = _run(args, Path(temporary))
    else:
        work = args.work_directory.expanduser().resolve(strict=False)
        if work.exists():
            raise SystemExit(f"benchmark work directory already exists: {work}")
        work.mkdir(parents=True)
        result = _run(args, work)
        if not args.keep_data:
            shutil.rmtree(work)
    _write_json(output, result)
    print(f"Benchmark result: {output}")
    for experiment in result["experiments"]:
        print(
            "transactions={transaction_count} model={model} family={feature_family} "
            "train_seconds={training_seconds:.6f} test_metric={primary_metric_value:.6f}".format(
                **experiment
            )
        )
    return 0


def _run(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    preparations: list[dict[str, Any]] = []
    for count in args.transactions:
        run = work / f"transactions-{count}"
        run.mkdir()
        bundle = run / "bundle"
        generation = write_scenario_bundle(
            bundle,
            ScenarioConfig(
                transaction_count=count,
                seed=args.seed,
                group_size=args.group_size,
            ),
        )
        canonical = run / "dataset"
        ingestion = ingest_file(bundle / "source.json", canonical)
        features = run / "features"
        feature_summary = build_features_v1(canonical, features)
        preparations.append(
            {
                "transaction_count": count,
                "source_observations": generation.observation_count,
                "accepted_observations": ingestion.records_accepted,
                "scenario_counts": generation.scenario_counts,
                "feature_dataset_id": feature_summary.feature_dataset_id,
                "excluded_from_ml_measurements": True,
            }
        )
        for experiment_type, model, family in _EXPERIMENTS:
            results.append(
                _run_worker(
                    count,
                    features,
                    bundle / "scenario-truth.json",
                    run / "experiments" / f"{model}-{family}",
                    experiment_type,
                    model,
                    family,
                    args.seed,
                )
            )
    return {
        "benchmark_version": _BENCHMARK_VERSION,
        "environment": _environment(),
        "configuration": {
            "transaction_counts": args.transactions,
            "seed": args.seed,
            "group_size": args.group_size,
            "split_strategy": "group",
            "feature_mode": "snapshot",
            "worker_isolation": "one fresh process per experiment",
            "preparation": "scenario generation, ingestion, and features excluded",
        },
        "dataset_preparation": preparations,
        "experiments": results,
    }


def _run_worker(
    transaction_count: int,
    features: Path,
    truth: Path,
    output: Path,
    experiment_type: str,
    model: str,
    feature_family: str,
    seed: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--transactions",
        str(transaction_count),
        "--features",
        str(features),
        "--truth",
        str(truth),
        "--output",
        str(output),
        "--experiment-type",
        experiment_type,
        "--model",
        model,
        "--feature-family",
        feature_family,
        "--seed",
        str(seed),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            f"ML worker failed for {model}/{feature_family}: {completed.stderr.strip()}"
        )
    return dict(json.loads(completed.stdout))


def _worker_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--transactions", type=int, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment-type", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--feature-family", required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(arguments)
    summary = run_experiment(
        ExperimentConfig(
            feature_path=args.features,
            truth_path=args.truth,
            output_root=args.output,
            experiment_type=args.experiment_type,
            model=args.model,
            feature_family=args.feature_family,
            split_strategy="group",
            seed=args.seed,
        )
    )
    manifest = json.loads((summary.output_path / "experiment.json").read_text(encoding="utf-8"))
    split_metadata = json.loads((summary.output_path / "split.json").read_text(encoding="utf-8"))
    artifact_size = sum(
        path.stat().st_size for path in summary.output_path.rglob("*") if path.is_file()
    )
    result = {
        "transaction_count": args.transactions,
        "entity_type": "transaction",
        "feature_count": summary.feature_count,
        "feature_family": args.feature_family,
        "split_strategy": "group",
        "split_counts": summary.split_counts,
        "group_audit": split_metadata["group_audit"],
        "experiment_type": args.experiment_type,
        "model": args.model,
        "training_seconds": manifest["runtime"]["training_seconds"],
        "inference_seconds": manifest["runtime"]["inference_seconds"],
        "peak_rss_bytes": manifest["runtime"]["process_peak_rss_bytes"],
        "artifact_size_bytes": artifact_size,
        "primary_metric_name": summary.primary_metric_name,
        "primary_metric_value": summary.primary_metric_value,
        "test_metrics": manifest["metrics"]["test"],
        "experiment_id": summary.experiment_id,
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or "not reported",
        "logical_cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "numpy_version": numpy.__version__,
        "pyarrow_version": pyarrow.__version__,
        "scikit_learn_version": sklearn.__version__,
        "joblib_version": joblib.__version__,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
