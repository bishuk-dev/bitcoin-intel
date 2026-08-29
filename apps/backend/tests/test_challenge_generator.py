from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from bitcoin_intel.benchmarking.challenge import (
    CHALLENGE_PROFILE,
    ChallengeConfig,
    audit_challenge_bundle,
    write_challenge_bundle,
)


def test_challenge_v1_is_deterministic_overlapping_and_truth_isolated(tmp_path: Path) -> None:
    config = ChallengeConfig(transaction_count=600, seed=71, group_size=12)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_summary = write_challenge_bundle(first, config)
    write_challenge_bundle(second, config)

    assert (first / "source.json").read_bytes() == (second / "source.json").read_bytes()
    assert (first / "scenario-truth.json").read_bytes() == (
        second / "scenario-truth.json"
    ).read_bytes()
    assert first_summary.profile == CHALLENGE_PROFILE
    assert first_summary.fingerprint_audit["status"] == "passed"

    records = json.loads((first / "source.json").read_text(encoding="utf-8"))
    truth = json.loads((first / "scenario-truth.json").read_text(encoding="utf-8"))
    labels = {row["txid"]: row["scenario_class"] for row in truth["transactions"]}
    output_counts: defaultdict[str, set[int]] = defaultdict(set)
    values: defaultdict[str, list[float]] = defaultdict(list)
    seen: set[str] = set()
    for record in records:
        assert {
            "scenario_class",
            "scenario_group_id",
            "scenario_intensity",
            "secondary_tags",
        }.isdisjoint(record)
        txid = record["txid"]
        if txid not in seen:
            seen.add(txid)
            output_counts[labels[txid]].add(len(record["output_addresses"]))
            values[labels[txid]].append(sum(map(float, record["input_amounts"])))

    assert output_counts["baseline"] & output_counts["high_fan_out_pattern"]
    baseline_range = min(values["baseline"]), max(values["baseline"])
    high_value_range = min(values["high_value_pattern"]), max(values["high_value_pattern"])
    assert baseline_range[0] < high_value_range[1] and high_value_range[0] < baseline_range[1]
    assert any(row["secondary_tags"] for row in truth["transactions"])
    assert {row["scenario_intensity"] for row in truth["transactions"]} >= {
        "weak",
        "medium",
        "strong",
    }


def test_challenge_related_identities_remain_inside_one_group(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_challenge_bundle(bundle, ChallengeConfig(transaction_count=300, seed=19))
    assert audit_challenge_bundle(bundle)["cross_group_identifier_count"] == 0
