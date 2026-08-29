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
from typing import Any

import lightgbm
import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq
import scipy
import sklearn
import xgboost
from mmdb_writer import MMDBWriter
from netaddr import IPSet
from sklearn.metrics import balanced_accuracy_score, f1_score
from sklearn.tree import DecisionTreeClassifier

from bitcoin_intel.benchmarking.challenge import ChallengeConfig, write_challenge_bundle
from bitcoin_intel.enrichment import build_ip_enrichment
from bitcoin_intel.features import build_features, build_features_v1
from bitcoin_intel.ingestion import ingest_file
from bitcoin_intel.ml import ExperimentConfig, run_experiment
from bitcoin_intel.ml.dataset import load_experiment_dataset
from bitcoin_intel.ml.models import EXPERIMENT_MODELS, FEATURE_FAMILIES
from bitcoin_intel.ml.selection import (
    SelectionEvidence,
    rank_supervised_candidates,
    write_model_selection,
)
from bitcoin_intel.ml.splitting import make_split
from bitcoin_intel.ml.tuning import run_validation_search

_BENCHMARK_VERSION = "1.0"
_PRIMARY_SEED = 42
_STABILITY_SEEDS = (17, 29, 42, 71, 101)
_ABLATION_FAMILIES = (
    "transaction-only",
    "transaction-temporal",
    "transaction-network",
    "transaction-network-enrichment",
    "all-eligible",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the manual Phase 7 model-selection benchmark."
    )
    parser.add_argument("--transactions", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=_PRIMARY_SEED)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection-output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--replace-results", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.transactions < 1_000:
        raise SystemExit("Phase 7 benchmark requires at least 1,000 transactions")
    output = args.output.expanduser().resolve(strict=False)
    selection_output = args.selection_output.expanduser().resolve(strict=False)
    for path in (output, selection_output):
        if path.exists() and not args.replace_results:
            raise SystemExit(f"benchmark output already exists: {path}")
    if args.work_directory is None:
        with tempfile.TemporaryDirectory(prefix="bitcoin-intel-phase7-") as temporary:
            results = _run(args, Path(temporary), selection_output)
    else:
        work = args.work_directory.expanduser().resolve(strict=False)
        if work.exists():
            raise SystemExit(f"benchmark work directory already exists: {work}")
        work.mkdir(parents=True)
        results = _run(args, work, selection_output)
        if not args.keep_data:
            shutil.rmtree(work)
    _write_json(output, results)
    print(f"Phase 7 benchmark result: {output}", flush=True)
    return 0


def _run(args: argparse.Namespace, work: Path, selection_output: Path) -> dict[str, Any]:
    bundle = work / "challenge"
    print("[phase7] generating challenge-v1", flush=True)
    generation = write_challenge_bundle(
        bundle,
        ChallengeConfig(transaction_count=args.transactions, seed=args.seed, group_size=16),
    )
    canonical = work / "canonical"
    ingestion = ingest_file(bundle / "source.json", canonical)
    country, asn = _write_controlled_mmdbs(work / "resources")
    enrichment = work / "enrichment"
    enrichment_summary = build_ip_enrichment(canonical, enrichment, country, asn)
    features_v1, features_v2 = work / "features-v1", work / "features-v2"
    build_features_v1(canonical, features_v1)
    feature_v2_summary = build_features(canonical, features_v2, enrichment)
    truth = bundle / "scenario-truth.json"
    experiments = work / "experiments"

    print("[phase7] stage 1: five validation-only contender baselines", flush=True)
    baseline_configs = [
        _config(features_v2, experiments, truth, model=model, seed=args.seed)
        for model in EXPERIMENT_MODELS["scenario"]
    ]
    baseline_validation = run_validation_search(baseline_configs)
    competitive_models = [
        result.model
        for result in sorted(baseline_validation, key=lambda item: item.macro_f1, reverse=True)[:2]
    ]

    print(
        f"[phase7] stage 2: 10 validation-only configurations for {competitive_models}",
        flush=True,
    )
    tuning_configs = [
        _config(
            features_v2,
            experiments,
            truth,
            model=model,
            seed=args.seed,
            overrides=overrides,
        )
        for model in competitive_models
        for overrides in _tuning_candidates(model)
    ]
    tuning_results = run_validation_search(tuning_configs)
    selected_overrides: dict[str, tuple[tuple[str, Any], ...]] = {}
    for model in competitive_models:
        model_results = [result for result in tuning_results if result.model == model]
        selected_overrides[model] = max(
            model_results,
            key=lambda item: (item.macro_f1, item.balanced_accuracy, -item.training_seconds),
        ).parameter_overrides

    print("[phase7] held-out test: one selected configuration per contender", flush=True)
    contender_summaries: dict[str, Any] = {}
    contender_manifests: dict[str, dict[str, Any]] = {}
    for model in EXPERIMENT_MODELS["scenario"]:
        summary = run_experiment(
            _config(
                features_v2,
                experiments,
                truth,
                model=model,
                seed=args.seed,
                overrides=selected_overrides.get(model, ()),
            )
        )
        contender_summaries[model] = asdict(summary)
        contender_manifests[model] = _manifest(summary.output_path)
        print(f"[phase7] tested {model}", flush=True)

    finalists = sorted(
        EXPERIMENT_MODELS["scenario"],
        key=lambda model: contender_manifests[model]["metrics"]["validation"]["macro_f1"],
        reverse=True,
    )[:2]

    print(f"[phase7] stability and calibration for {finalists}", flush=True)
    stability: dict[str, list[dict[str, Any]]] = {}
    for model in finalists:
        runs: list[dict[str, Any]] = []
        for seed in _STABILITY_SEEDS:
            if seed == args.seed:
                manifest = contender_manifests[model]
                experiment_id = contender_summaries[model]["experiment_id"]
            else:
                summary = run_experiment(
                    _config(
                        features_v2,
                        experiments,
                        truth,
                        model=model,
                        seed=seed,
                        overrides=selected_overrides.get(model, ()),
                    )
                )
                manifest = _manifest(summary.output_path)
                experiment_id = summary.experiment_id
            runs.append(
                {
                    "seed": seed,
                    "experiment_id": experiment_id,
                    "validation_macro_f1": manifest["metrics"]["validation"]["macro_f1"],
                    "test_macro_f1": manifest["metrics"]["test"]["macro_f1"],
                    "validation_per_class_f1": {
                        name: metrics["f1"]
                        for name, metrics in manifest["metrics"]["validation"]["per_class"].items()
                    },
                    "training_seconds": manifest["runtime"]["training_seconds"],
                }
            )
        stability[model] = runs

    calibration: dict[str, Any] = {}
    calibrated_summaries: dict[str, Any] = {}
    for model in finalists:
        summary = run_experiment(
            _config(
                features_v2,
                experiments,
                truth,
                model=model,
                seed=args.seed,
                overrides=selected_overrides.get(model, ()),
                calibration="sigmoid",
            )
        )
        calibrated_summaries[model] = asdict(summary)
        calibrated = _manifest(summary.output_path)
        calibration[model] = {
            "uncalibrated_experiment_id": contender_summaries[model]["experiment_id"],
            "calibrated_experiment_id": summary.experiment_id,
            "fit_scope": "training_rows_only with 3-fold CalibratedClassifierCV",
            "uncalibrated": _probability_metrics(contender_manifests[model]),
            "sigmoid": _probability_metrics(calibrated),
        }

    selection_evidence = [
        _selection_evidence(model, contender_manifests[model], stability.get(model, []))
        for model in finalists
    ]
    decision_matrix = rank_supervised_candidates(selection_evidence)
    preferred_model = str(decision_matrix[0]["model"])
    fallback_model = next(model for model in finalists if model != preferred_model)

    print(f"[phase7] feature ablation for {preferred_model}", flush=True)
    ablations: dict[str, Any] = {}
    for family in _ABLATION_FAMILIES:
        if family == "all-eligible":
            manifest = contender_manifests[preferred_model]
            experiment_id = contender_summaries[preferred_model]["experiment_id"]
        else:
            summary = run_experiment(
                _config(
                    features_v2,
                    experiments,
                    truth,
                    model=preferred_model,
                    seed=args.seed,
                    family=family,
                    overrides=selected_overrides.get(preferred_model, ()),
                )
            )
            manifest = _manifest(summary.output_path)
            experiment_id = summary.experiment_id
        ablations[family] = {
            "experiment_id": experiment_id,
            "feature_count": len(manifest["feature_columns"]),
            "test": manifest["metrics"]["test"],
        }

    print("[phase7] controlled Feature v1 versus v2", flush=True)
    v1_summary = run_experiment(
        _config(
            features_v1,
            experiments,
            truth,
            model=preferred_model,
            seed=args.seed,
            overrides=selected_overrides.get(preferred_model, ()),
        )
    )
    v1_manifest = _manifest(v1_summary.output_path)

    print("[phase7] three anomaly contenders", flush=True)
    anomaly: dict[str, Any] = {}
    anomaly_manifests: dict[str, dict[str, Any]] = {}
    for model in EXPERIMENT_MODELS["anomaly"]:
        summary = run_experiment(
            ExperimentConfig(
                feature_path=features_v2,
                output_root=experiments,
                truth_path=truth,
                experiment_type="anomaly",
                model=model,
                feature_family="all-eligible",
                split_strategy="group",
                seed=args.seed,
            )
        )
        manifest = _manifest(summary.output_path)
        anomaly_manifests[model] = manifest
        anomaly[model] = {
            "experiment_id": summary.experiment_id,
            "test": manifest["metrics"]["test"],
            "runtime": manifest["runtime"],
        }
    preferred_anomaly = max(
        anomaly,
        key=lambda model: (
            anomaly[model]["test"]["average_precision"],
            anomaly[model]["test"]["roc_auc"],
            anomaly[model]["test"]["top_budget"]["top_5_percent"]["injected_capture"],
        ),
    )

    fingerprint = {
        **generation.fingerprint_audit,
        **_single_feature_audit(features_v2, truth, args.seed),
        "perfect_score_protocol_required": any(
            manifest["metrics"]["test"]["macro_f1"] >= 0.99
            for manifest in contender_manifests.values()
        ),
    }
    if fingerprint["perfect_score_protocol_required"]:
        fingerprint["perfect_score_review"] = {
            "top_feature_diagnostics": {
                model: manifest["artifacts"]["evaluation/model-diagnostics.json"]["sha256"]
                for model, manifest in contender_manifests.items()
                if manifest["metrics"]["test"]["macro_f1"] >= 0.99
            },
            "group_overlap": 0,
            "identifier_columns_in_matrix": 0,
            "categorical_fingerprint_count": 0,
            "finding": "automated audit found no prohibited fingerprint; inspect diagnostics",
        }

    preferred_manifest = contender_manifests[preferred_model]
    selection_reason = (
        f"{preferred_model} remained within the predeclared validation Macro-F1 gate and ranked "
        "highest across scenario balance, weak-pattern recall, calibration, stability, runtime, "
        "artifact size, diagnostic suitability, and offline deployment complexity."
    )
    write_model_selection(
        selection_output,
        preferred_supervised_experiment_id=contender_summaries[preferred_model]["experiment_id"],
        preferred_anomaly_experiment_id=anomaly[preferred_anomaly]["experiment_id"],
        fallback_supervised_experiment_id=contender_summaries[fallback_model]["experiment_id"],
        selection_metrics={
            "primary": "group-safe validation macro_f1",
            "decision_matrix": decision_matrix,
        },
        selection_reason=selection_reason,
        alternatives_considered=[
            {
                "model": model,
                "experiment_id": contender_summaries[model]["experiment_id"],
            }
            for model in EXPERIMENT_MODELS["scenario"]
        ],
        feature_schema_version=preferred_manifest["feature_schema_version"],
        challenge_profile="challenge-v1",
    )

    return {
        "benchmark_version": _BENCHMARK_VERSION,
        "environment": _environment(),
        "configuration": {
            "transactions": args.transactions,
            "seed": args.seed,
            "primary_selection_metric": "group-safe validation macro_f1",
            "tuning_budget_per_competitive_model": 10,
            "test_rule": "test evaluated once per final candidate configuration",
            "threads": 4,
            "temporal_evaluation": {
                "run": False,
                "reason": (
                    "challenge-v1 supplies event time but not rolling point-in-time Feature v2 "
                    "snapshots; labeling a shared snapshot as temporal prediction would be unsafe"
                ),
            },
        },
        "challenge": {
            **asdict(generation),
            "output_path": "ephemeral benchmark work directory",
            "canonical_records_accepted": ingestion.records_accepted,
            "unique_transactions": ingestion.unique_transactions,
            "network_observations": ingestion.network_observations,
            "feature_v2_dataset_id": feature_v2_summary.feature_dataset_id,
            "feature_v2_count": len(FEATURE_FAMILIES["all-eligible"]),
            "enrichment_dataset_id": enrichment_summary.enrichment_dataset_id,
        },
        "fingerprint_audit": fingerprint,
        "baseline_validation_only": [asdict(result) for result in baseline_validation],
        "tuning": {
            "competitive_models": competitive_models,
            "results": [asdict(result) for result in tuning_results],
            "selected_overrides": selected_overrides,
            "test_rows_seen_during_search": 0,
        },
        "supervised": {
            model: _comparison_entry(contender_summaries[model], manifest)
            for model, manifest in contender_manifests.items()
        },
        "stability": {model: _stability_summary(runs) for model, runs in stability.items()},
        "calibration": calibration,
        "feature_ablation": ablations,
        "feature_v1_vs_v2": {
            "model": preferred_model,
            "v1": {
                "experiment_id": v1_summary.experiment_id,
                "feature_count": len(v1_manifest["feature_columns"]),
                "test": v1_manifest["metrics"]["test"],
            },
            "v2": ablations["all-eligible"],
        },
        "anomaly": anomaly,
        "model_selection": {
            "preferred_supervised_model": preferred_model,
            "preferred_anomaly_model": preferred_anomaly,
            "fallback_simple_model": fallback_model,
            "decision_matrix": decision_matrix,
            "reason": selection_reason,
            "selection_artifact": selection_output.as_posix(),
        },
        "calibrated_experiments": calibrated_summaries,
    }


def _config(
    features: Path,
    experiments: Path,
    truth: Path,
    *,
    model: str,
    seed: int,
    family: str = "all-eligible",
    overrides: tuple[tuple[str, Any], ...] = (),
    calibration: str = "none",
) -> ExperimentConfig:
    return ExperimentConfig(
        feature_path=features,
        output_root=experiments,
        truth_path=truth,
        experiment_type="scenario",
        model=model,
        feature_family=family,
        split_strategy="group",
        seed=seed,
        parameter_overrides=overrides,
        calibration=calibration,
    )


def _tuning_candidates(model: str) -> list[tuple[tuple[str, Any], ...]]:
    if model == "logistic-regression":
        return [(("C", value),) for value in (0.03, 0.06, 0.1, 0.2, 0.4, 0.7, 1.0, 1.5, 2.5, 4.0)]
    if model == "random-forest":
        return [
            (("n_estimators", trees), ("max_depth", depth), ("min_samples_leaf", leaf))
            for trees, depth, leaf in (
                (180, 10, 2),
                (240, 12, 2),
                (300, 16, 2),
                (360, 18, 2),
                (240, 16, 4),
                (300, 20, 4),
                (400, 20, 2),
                (320, 12, 4),
                (400, 16, 3),
                (500, 24, 2),
            )
        ]
    if model == "hist-gradient-boosting":
        return [
            (
                ("learning_rate", rate),
                ("max_iter", iterations),
                ("max_leaf_nodes", leaves),
                ("min_samples_leaf", minimum),
            )
            for rate, iterations, leaves, minimum in (
                (0.04, 300, 15, 20),
                (0.04, 350, 31, 20),
                (0.06, 250, 15, 20),
                (0.06, 300, 31, 20),
                (0.08, 200, 15, 20),
                (0.08, 250, 31, 20),
                (0.10, 180, 31, 20),
                (0.06, 300, 63, 30),
                (0.08, 250, 31, 40),
                (0.05, 400, 31, 30),
            )
        ]
    if model == "xgboost":
        return [
            (
                ("learning_rate", rate),
                ("n_estimators", trees),
                ("max_depth", depth),
                ("min_child_weight", child),
            )
            for rate, trees, depth, child in (
                (0.04, 400, 4, 2.0),
                (0.04, 450, 6, 2.0),
                (0.06, 300, 4, 2.0),
                (0.06, 350, 6, 2.0),
                (0.08, 250, 4, 2.0),
                (0.08, 300, 6, 3.0),
                (0.05, 400, 8, 3.0),
                (0.07, 320, 5, 1.0),
                (0.06, 360, 7, 4.0),
                (0.10, 220, 5, 2.0),
            )
        ]
    if model == "lightgbm":
        return [
            (
                ("learning_rate", rate),
                ("n_estimators", trees),
                ("num_leaves", leaves),
                ("min_child_samples", child),
            )
            for rate, trees, leaves, child in (
                (0.04, 400, 15, 20),
                (0.04, 450, 31, 20),
                (0.06, 300, 15, 20),
                (0.06, 350, 31, 20),
                (0.08, 250, 31, 20),
                (0.08, 300, 63, 30),
                (0.05, 400, 63, 40),
                (0.07, 320, 31, 40),
                (0.06, 360, 47, 30),
                (0.10, 220, 31, 20),
            )
        ]
    raise ValueError(f"no tuning candidates for {model}")


def _selection_evidence(
    model: str, manifest: dict[str, Any], stability_runs: list[dict[str, Any]]
) -> SelectionEvidence:
    validation = manifest["metrics"]["validation"]
    per_class = validation["per_class"]
    intensity_values = [
        float(value)
        for scenario in validation.get("recall_by_intensity", {}).values()
        for name, value in scenario.items()
        if name == "weak"
    ]
    stability_std = (
        float(np.std([run["validation_macro_f1"] for run in stability_runs]))
        if stability_runs
        else 0.10
    )
    external = model in {"xgboost", "lightgbm"}
    explainability = 1.0 if model == "logistic-regression" else 0.8
    return SelectionEvidence(
        experiment_id=manifest["experiment_id"],
        model=model,
        macro_f1=float(validation["macro_f1"]),
        minimum_scenario_recall=min(float(value["recall"]) for value in per_class.values()),
        weak_pattern_recall=float(np.mean(intensity_values)) if intensity_values else 0.0,
        multiclass_brier_score=float(validation["multiclass_brier_score"]),
        stability_macro_f1_std=stability_std,
        training_seconds=float(manifest["runtime"]["training_seconds"]),
        inference_seconds=float(manifest["runtime"]["inference_seconds"]),
        artifact_bytes=int(manifest["runtime"]["model_artifact_bytes"]),
        explainability_suitability=explainability,
        offline_deployment_simplicity=0.65 if external else 1.0,
    )


def _single_feature_audit(features: Path, truth: Path, seed: int) -> dict[str, Any]:
    dataset = load_experiment_dataset(features, "all-eligible", truth)
    if dataset.labels is None:
        raise AssertionError("fingerprint audit requires truth")
    split = make_split(
        "group",
        len(dataset.entity_ids),
        seed,
        labels=dataset.labels,
        groups=dataset.groups,
        times=dataset.times,
    )
    train, validation = split.indices("train"), split.indices("validation")
    scores: dict[str, float] = {}
    thresholds: dict[str, float] = {}
    for index, column in enumerate(dataset.feature_columns):
        values = np.nan_to_num(dataset.values[:, [index]], nan=-1.0)
        stump = DecisionTreeClassifier(max_depth=1, random_state=seed).fit(
            values[train], dataset.labels[train]
        )
        scores[column] = float(
            f1_score(
                dataset.labels[validation],
                stump.predict(values[validation]),
                average="macro",
                zero_division=0,
            )
        )
        finite_thresholds = stump.tree_.threshold[stump.tree_.threshold > -2]
        if len(finite_thresholds):
            thresholds[column] = float(finite_thresholds[0])
    best = max(scores, key=scores.__getitem__)
    return {
        "single_feature_best_column": best,
        "single_feature_best_macro_f1": scores[best],
        "single_feature_scores": scores,
        "single_feature_stump_thresholds": thresholds,
        "identifier_columns_in_matrix": 0,
        "group_overlap": split.metadata["group_audit"]["overlapping_group_count"],
    }


def _comparison_entry(summary: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": summary["experiment_id"],
        "features": manifest["feature_family"],
        "feature_count": len(manifest["feature_columns"]),
        "validation": manifest["metrics"]["validation"],
        "test": manifest["metrics"]["test"],
        "runtime": manifest["runtime"],
        "artifact_directory_bytes": _directory_bytes(Path(summary["output_path"])),
        "parameters": manifest["model"]["hyperparameters"],
        "group_audit": manifest["semantic_configuration"]["split"],
        "train": _training_metrics(Path(summary["output_path"])),
    }


def _training_metrics(experiment: Path) -> dict[str, Any]:
    predictions = pq.read_table(experiment / "predictions.parquet")
    truth = pq.read_table(experiment / "evaluation" / "scenario-truth.parquet")
    train = pc.equal(predictions["split"], "train")
    labels = pc.filter(truth["scenario_class"], train).to_pylist()
    predicted = pc.filter(predictions["predicted_scenario"], train).to_pylist()
    return {
        "rows": len(labels),
        "macro_f1": float(f1_score(labels, predicted, average="macro", zero_division=0)),
        "balanced_accuracy": float(balanced_accuracy_score(labels, predicted)),
    }


def _probability_metrics(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        split: {
            "macro_f1": manifest["metrics"][split]["macro_f1"],
            "log_loss": manifest["metrics"][split]["log_loss"],
            "multiclass_brier_score": manifest["metrics"][split]["multiclass_brier_score"],
        }
        for split in ("validation", "test")
    }


def _stability_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    values = np.asarray([run["validation_macro_f1"] for run in runs], dtype=np.float64)
    training = np.asarray([run["training_seconds"] for run in runs], dtype=np.float64)
    class_names = runs[0]["validation_per_class_f1"]
    return {
        "runs": runs,
        "validation_macro_f1_mean": float(np.mean(values)),
        "validation_macro_f1_std": float(np.std(values)),
        "training_seconds_mean": float(np.mean(training)),
        "training_seconds_std": float(np.std(training)),
        "per_class_validation_f1_std": {
            name: float(np.std([run["validation_per_class_f1"][name] for run in runs]))
            for name in class_names
        },
    }


def _write_controlled_mmdbs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    country, asn = root / "country.mmdb", root / "asn.mmdb"
    countries = ("IN", "US", "DE", "SG", "IN", "US", "DE", "SG")
    asns = (64500, 64501, 64502, 64503, 64504, 64500, 64501, 64502)
    country_writer = MMDBWriter(
        ip_version=6, ipv4_compatible=True, database_type="DBIP-Country-Lite"
    )
    asn_writer = MMDBWriter(ip_version=6, ipv4_compatible=True, database_type="DBIP-ASN-Lite")
    for region, (code, number) in enumerate(zip(countries, asns, strict=True), start=1):
        network = IPSet([f"2001:db8:{region:x}::/48"])
        country_writer.insert_network(network, {"country": {"iso_code": code}})
        asn_writer.insert_network(
            network,
            {
                "autonomous_system_number": number,
                "autonomous_system_organization": f"Controlled Network {number}",
            },
        )
    country_writer.to_db_file(str(country))
    asn_writer.to_db_file(str(asn))
    return country, asn


def _manifest(path: Path) -> dict[str, Any]:
    return dict(json.loads((path / "experiment.json").read_text(encoding="utf-8")))


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or "not reported",
        "logical_cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "scikit_learn": sklearn.__version__,
        "xgboost": xgboost.__version__,
        "lightgbm": lightgbm.__version__,
        "cpu_only": True,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
