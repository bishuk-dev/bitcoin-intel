from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from bitcoin_intel.ml.artifacts import inspect_experiment
from bitcoin_intel.ml.models import (
    CALIBRATION_METHODS,
    EXPERIMENT_MODELS,
    FEATURE_FAMILIES,
    SPLIT_STRATEGIES,
    ExperimentConfig,
)
from bitcoin_intel.ml.training import run_experiment


def configure_ml_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "ml", help="train and inspect deterministic leakage-controlled baseline experiments"
    )
    commands = parser.add_subparsers(dest="ml_command", required=True)
    anomaly = commands.add_parser("train-anomaly", help="train an unsupervised anomaly baseline")
    _common_training_arguments(anomaly)
    anomaly.set_defaults(split="random-stratified")
    anomaly.add_argument("--truth", type=Path, help="evaluation-only synthetic truth sidecar")
    anomaly.add_argument(
        "--model",
        choices=EXPERIMENT_MODELS["anomaly"],
        default="isolation-forest",
    )

    scenario = commands.add_parser(
        "train-scenario", help="train a synthetic multiclass scenario baseline"
    )
    _common_training_arguments(scenario)
    scenario.add_argument("--truth", type=Path, required=True)
    scenario.add_argument(
        "--model",
        choices=EXPERIMENT_MODELS["scenario"],
        default="logistic-regression",
    )
    scenario.add_argument("--calibration", choices=CALIBRATION_METHODS, default="none")

    evaluate = commands.add_parser(
        "evaluate", help="verify and inspect an existing experiment without loading its model"
    )
    evaluate.add_argument("--experiment", type=Path, required=True)


def run_ml_command(args: argparse.Namespace) -> int:
    if args.ml_command == "evaluate":
        print(json.dumps(inspect_experiment(args.experiment), indent=2, sort_keys=True))
        return 0
    if args.ml_command in {"train-anomaly", "train-scenario"}:
        summary = run_experiment(
            ExperimentConfig(
                feature_path=args.features,
                output_root=args.output,
                experiment_type=("anomaly" if args.ml_command == "train-anomaly" else "scenario"),
                model=args.model,
                truth_path=args.truth,
                feature_family=args.feature_family,
                split_strategy=args.split,
                seed=args.seed,
                calibration=getattr(args, "calibration", "none"),
            )
        )
        print(json.dumps(asdict(summary), indent=2, sort_keys=True, default=str))
        return 0
    raise AssertionError("argparse accepted an unknown ML command")


def _common_training_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="experiment root directory")
    parser.add_argument("--feature-family", choices=tuple(FEATURE_FAMILIES), default="all-eligible")
    parser.add_argument("--split", choices=SPLIT_STRATEGIES, default="group")
    parser.add_argument("--seed", type=int, default=42)
