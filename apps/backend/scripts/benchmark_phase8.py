from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any

import igraph
import pyarrow
import sklearn

from bitcoin_intel.benchmarking.entity_challenge import (
    EntityChallengeConfig,
    write_entity_challenge_bundle,
)
from bitcoin_intel.entity.evaluation import evaluate_entity_store
from bitcoin_intel.entity.models import MANIFEST_FILE_NAME, EntityBuildConfig
from bitcoin_intel.entity.pipeline import build_entity_hypotheses
from bitcoin_intel.features import build_features_v1
from bitcoin_intel.ingestion import ingest_file

_BENCHMARK_VERSION = "1.0"
_CANDIDATES = (
    EntityBuildConfig(
        collaborative_min_inputs=3,
        collaborative_min_outputs=3,
        collaborative_min_equal_outputs=2,
        collaborative_min_equal_fraction=0.5,
    ),
    EntityBuildConfig(
        collaborative_min_inputs=4,
        collaborative_min_outputs=4,
        collaborative_min_equal_outputs=3,
        collaborative_min_equal_fraction=0.5,
    ),
    EntityBuildConfig(
        collaborative_min_inputs=4,
        collaborative_min_outputs=4,
        collaborative_min_equal_outputs=4,
        collaborative_min_equal_fraction=0.75,
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the manual Phase 8 entity benchmark.")
    parser.add_argument("--transactions", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--replace-results", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.transactions < 1_000:
        raise SystemExit("Phase 8 benchmark requires at least 1,000 transactions")
    output = args.output.expanduser().resolve(strict=False)
    if output.exists() and not args.replace_results:
        raise SystemExit(f"benchmark output already exists: {output}")
    if args.work_directory is None:
        with tempfile.TemporaryDirectory(prefix="bitcoin-intel-phase8-") as temporary:
            result = _run(args, Path(temporary))
    else:
        work = args.work_directory.expanduser().resolve(strict=False)
        if work.exists():
            raise SystemExit(f"benchmark work directory already exists: {work}")
        work.mkdir(parents=True)
        result = _run(args, work)
        if not args.keep_data:
            shutil.rmtree(work)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(f"Phase 8 benchmark result: {output}", flush=True)
    return 0


def _run(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    print("[phase8] generating entity-challenge-v1", flush=True)
    bundle = work / "challenge"
    generation = write_entity_challenge_bundle(
        bundle, EntityChallengeConfig(transaction_count=args.transactions, seed=args.seed)
    )
    canonical = work / "canonical"
    features = work / "features"
    preparation_started = perf_counter()
    ingestion = ingest_file(bundle / "source.json", canonical)
    feature_summary = build_features_v1(canonical, features)
    preparation_seconds = perf_counter() - preparation_started
    truth = bundle / "entity-truth.json"

    candidates: list[dict[str, Any]] = []
    for index, config in enumerate(_CANDIDATES):
        print(f"[phase8] validation-only detector candidate {index + 1}", flush=True)
        entity_path = work / f"entity-candidate-{index}"
        started = perf_counter()
        summary = build_entity_hypotheses(canonical, features, entity_path, config)
        build_seconds = perf_counter() - started
        development = evaluate_entity_store(canonical, entity_path, truth, partition="development")
        validation = evaluate_entity_store(canonical, entity_path, truth, partition="validation")
        summary_data = asdict(summary)
        summary_data["output_path"] = f"entity-candidate-{index}"
        candidates.append(
            {
                "index": index,
                "configuration": config.semantic_dict(),
                "build_seconds": build_seconds,
                "summary": summary_data,
                "development": _compact_evaluation(development),
                "validation": _compact_evaluation(validation),
                "path": entity_path,
            }
        )
    selected = max(
        candidates,
        key=lambda row: (
            row["validation"]["baselines"]["final_conservative"]["pairwise_precision"],
            -row["validation"]["baselines"]["final_conservative"]["collaborative_false_merge_rate"],
            row["validation"]["baselines"]["final_conservative"]["pairwise_f1"],
        ),
    )
    selected_path = selected.pop("path")
    for row in candidates:
        row.pop("path", None)
    print(
        f"[phase8] configuration frozen at candidate {selected['index']}; opening test", flush=True
    )
    test = evaluate_entity_store(canonical, selected_path, truth, partition="test")

    repeat = work / "entity-selected-repeat"
    repeat_started = perf_counter()
    build_entity_hypotheses(
        canonical,
        features,
        repeat,
        EntityBuildConfig(**selected["configuration"]),
    )
    repeat_seconds = perf_counter() - repeat_started
    first_manifest = _manifest(selected_path)
    repeat_manifest = _manifest(repeat)
    first_hashes = {
        name: metadata["sha256"] for name, metadata in first_manifest["output_tables"].items()
    }
    repeat_hashes = {
        name: metadata["sha256"] for name, metadata in repeat_manifest["output_tables"].items()
    }
    generation_data = asdict(generation)
    generation_data["output_path"] = "challenge"
    return {
        "benchmark_version": _BENCHMARK_VERSION,
        "phase": 8,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "pyarrow": pyarrow.__version__,
            "scikit_learn": sklearn.__version__,
            "igraph": igraph.__version__,
        },
        "configuration": {
            "transactions": args.transactions,
            "seed": args.seed,
            "selection_policy": (
                "validation pairwise precision, then lower collaborative false-merge rate, "
                "then pairwise F1"
            ),
            "test_access_policy": "test evaluated once after detector configuration freeze",
            "feature_schema": "1.0.0 (address-feature columns are identical to v2)",
        },
        "generation": generation_data,
        "preparation": {
            "seconds": preparation_seconds,
            "accepted_observations": ingestion.records_accepted,
            "unique_transactions": ingestion.unique_transactions,
            "feature_rows": feature_summary.table_rows,
            "excluded_from_entity_build_timing": True,
        },
        "validation_candidates": candidates,
        "selection": {
            "selected_candidate_index": selected["index"],
            "selected_configuration": selected["configuration"],
            "selection_partition": "validation",
        },
        "held_out_test": _compact_evaluation(test),
        "selected_artifact": {
            "entity_dataset_id": first_manifest["entity_dataset_id"],
            "output_rows": {
                name: metadata["rows"] for name, metadata in first_manifest["output_tables"].items()
            },
            "output_bytes": sum(
                metadata["bytes"] for metadata in first_manifest["output_tables"].values()
            ),
            "repeat_build_seconds": repeat_seconds,
            "repeat_entity_dataset_id_equal": (
                first_manifest["entity_dataset_id"] == repeat_manifest["entity_dataset_id"]
            ),
            "repeat_parquet_hashes_equal": first_hashes == repeat_hashes,
        },
        "claim_limit": (
            "Synthetic ownership truth evaluates heuristics only; it does not establish real-world "
            "ownership, identity, control, guilt, or criminality."
        ),
    }


def _compact_evaluation(result: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in result.items() if key != "baselines"}
    compact["baselines"] = {
        name: {key: value for key, value in metrics.items() if key != "per_entity"}
        for name, metrics in result["baselines"].items()
    }
    return compact


def _manifest(path: Path) -> dict[str, Any]:
    return dict(json.loads((path / MANIFEST_FILE_NAME).read_text(encoding="utf-8")))


if __name__ == "__main__":
    raise SystemExit(main())
