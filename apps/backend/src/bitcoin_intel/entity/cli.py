from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from bitcoin_intel.entity.evaluation import evaluate_entity_store
from bitcoin_intel.entity.models import EntityBuildConfig
from bitcoin_intel.entity.pipeline import build_entity_hypotheses
from bitcoin_intel.entity.validation import validate_entity_store


def configure_entity_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "entity", help="build, validate, or evaluate conservative entity hypotheses"
    )
    commands = parser.add_subparsers(dest="entity_command", required=True)
    build = commands.add_parser("build", help="build a new atomic entity-hypothesis store")
    build.add_argument("--dataset", type=Path, required=True)
    build.add_argument("--features", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--collaborative-min-inputs", type=int, default=3)
    build.add_argument("--collaborative-min-outputs", type=int, default=3)
    build.add_argument("--collaborative-min-equal-outputs", type=int, default=2)
    build.add_argument("--collaborative-min-equal-fraction", type=float, default=0.5)
    build.add_argument("--behavioral-min-cluster-size", type=int, default=5)
    build.add_argument("--behavioral-min-samples", type=int, default=3)
    build.add_argument("--leiden-seed", type=int, default=42)
    validate = commands.add_parser("validate", help="validate entity artifacts and lineage")
    validate.add_argument("--entities", type=Path, required=True)
    validate.add_argument("--dataset", type=Path, required=True)
    validate.add_argument("--features", type=Path, required=True)
    evaluate = commands.add_parser(
        "evaluate", help="compare raw, suppressed, and final clustering against hidden truth"
    )
    evaluate.add_argument("--entities", type=Path, required=True)
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--features", type=Path, required=True)
    evaluate.add_argument("--truth", type=Path, required=True)
    evaluate.add_argument(
        "--partition", choices=("development", "validation", "test"), default="test"
    )


def run_entity_command(args: argparse.Namespace) -> int:
    if args.entity_command == "build":
        summary = build_entity_hypotheses(
            args.dataset,
            args.features,
            args.output,
            EntityBuildConfig(
                collaborative_min_inputs=args.collaborative_min_inputs,
                collaborative_min_outputs=args.collaborative_min_outputs,
                collaborative_min_equal_outputs=args.collaborative_min_equal_outputs,
                collaborative_min_equal_fraction=args.collaborative_min_equal_fraction,
                behavioral_min_cluster_size=args.behavioral_min_cluster_size,
                behavioral_min_samples=args.behavioral_min_samples,
                leiden_seed=args.leiden_seed,
            ),
        )
        print(json.dumps(asdict(summary), indent=2, sort_keys=True, default=str))
        return 0
    if args.entity_command == "validate":
        report = validate_entity_store(args.entities, args.dataset, args.features)
        print(
            json.dumps(
                {"valid": report.is_valid, "issues": [asdict(issue) for issue in report.issues]},
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report.is_valid else 2
    if args.entity_command == "evaluate":
        report = validate_entity_store(args.entities, args.dataset, args.features)
        if not report.is_valid:
            details = ", ".join(issue.code for issue in report.issues)
            raise ValueError(f"entity store failed validation: {details}")
        result = evaluate_entity_store(
            args.dataset, args.entities, args.truth, partition=args.partition
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    raise AssertionError("argparse accepted an unknown entity command")
