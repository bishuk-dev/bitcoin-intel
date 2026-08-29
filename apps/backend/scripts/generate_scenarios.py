from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from bitcoin_intel.benchmarking.scenarios import (
    DEFAULT_SCENARIO_PROPORTIONS,
    ScenarioConfig,
    write_scenario_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic connected intelligence-test scenario bundle."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transactions", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--group-size", type=int, default=20)
    parser.add_argument(
        "--scenario-proportion",
        action="append",
        default=[],
        metavar="NAME=FRACTION",
        help="repeat to replace the default non-baseline scenario proportions",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    proportions = (
        tuple(_parse_proportion(value) for value in args.scenario_proportion)
        if args.scenario_proportion
        else DEFAULT_SCENARIO_PROPORTIONS
    )
    summary = write_scenario_bundle(
        args.output,
        ScenarioConfig(
            transaction_count=args.transactions,
            seed=args.seed,
            group_size=args.group_size,
            scenario_proportions=proportions,
        ),
    )
    print(json.dumps(asdict(summary), indent=2, sort_keys=True, default=str))
    return 0


def _parse_proportion(value: str) -> tuple[str, float]:
    name, separator, raw_fraction = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError("scenario proportion must use NAME=FRACTION")
    try:
        fraction = float(raw_fraction)
    except ValueError as error:
        raise argparse.ArgumentTypeError("scenario fraction must be numeric") from error
    return name, fraction


if __name__ == "__main__":
    raise SystemExit(main())
