# Synthetic Intelligence-Test Scenarios

The Phase 2 benchmark generator remains an infrastructure workload with mostly disconnected
transactions. Phase 4 adds a separate deterministic scenario generator for feature correctness and
future evaluation of connected structures. It is designed for analytical variety, not statistical
fidelity to the Bitcoin network.

## Bundle contract

```bash
cd apps/backend
uv run python scripts/generate_scenarios.py \
  --transactions 1000 --seed 42 --output ../../benchmarks/generated/scenarios-1000
```

The destination is new and atomically published as:

```text
source.json             Phase 1-compatible input records
scenario-truth.json     evaluation-only sidecar
```

The same seed, transaction count, and normalized scenario proportions produce identical bytes and
SHA-256 hashes. The writer streams both arrays, so memory does not grow with all generated records.
Use repeated `--scenario-proportion NAME=FRACTION` arguments to replace defaults; configured
non-baseline proportions must sum to at most 1 and the remainder is baseline.

## Structural variety

All generated transactions form address chains. Deterministic index patterns additionally create
reused input addresses, a hub-like address, multi-input and multi-output transactions, occasional
input/output self-appearance, repeated observations, changing endpoints, IPv4 and IPv6 endpoints,
shared IPs, bursty timestamps, and observations separated by seven days.

The evaluation sidecar uses these non-normative scenario classes:

- `baseline`
- `high_fan_out_pattern`
- `rapid_sequence_pattern`
- `shared_network_pattern`
- `high_value_pattern`

These names describe generator mechanics. They are not criminal, malicious, ransomware, darknet,
or money-laundering ground truth.

## Ground-truth separation

`source.json` contains only the Phase 1 source contract. Scenario class, structural truth, and the
explicit `not_criminal_ground_truth` marker exist only in `scenario-truth.json`. Canonical
ingestion, graph import, and the feature pipeline receive only `source.json`; feature code has no
sidecar parameter or reader.

An end-to-end leakage regression test ingests a scenario bundle, prepares the factual graph import,
builds features, and verifies that scenario/truth field names are absent from every canonical,
graph-import, and feature Parquet schema.
