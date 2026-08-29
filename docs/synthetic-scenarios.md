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

Related structures are contained within deterministic groups (20 transactions by default). Use
`--group-size` to change that contract. Address chains restart for each group, and reused inputs,
hubs, and endpoint pools include the group identity so they cannot connect different groups. The
truth sidecar records `scenario_group_id`; source records do not. This supports group-aware ML
splits without turning generator metadata into a predictor.

## Structural variety

Transactions form address chains within their scenario groups. Deterministic index patterns also create
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

## Phase 7 challenge-v1 profile

The historical generator above remains unchanged for regression and Phase 5 reproducibility.
Select the harder, separately versioned profile explicitly:

```bash
uv run python scripts/generate_scenarios.py \
  --profile challenge-v1 --transactions 20000 --seed 42 --group-size 16 \
  --output ../../benchmarks/generated/challenge-v1-20000
```

`challenge-v1` replaces fixed extremes with overlapping distributions. Baseline transactions can
naturally have high values, moderate fan-out, bursts, endpoint reuse, multiple inputs/outputs,
address reuse, and hub/chain behaviour. Injected scenarios shift those same distributions at
deterministic weak, medium, or strong intensity; they do not receive arbitrary label noise. About
22% of rows also receive an evaluation-only secondary behavioural tag while retaining exactly one
primary multiclass label.

Transaction order is a deterministic permutation rather than class order. TXIDs and group tokens
are hashes without scenario text. All addresses and exact IPs remain group-local. Ports and script
types come from common pools, timestamps are independent of class, and eight enrichment prefixes
are shared by every class. Countries/ASNs therefore describe overlapping endpoint populations,
while match, diversity, and reuse behaviour can differ.

The `1.2.0` truth sidecar adds `scenario_intensity`, `secondary_tags`, and profile identity. Those
fields remain evaluation-only. An audit rejects truth keys in source, scenario names in IDs,
cross-group identifiers, single-class port/script/IP-prefix values, and implausible class ordering.
The measured 20k profile has no such fingerprint; its best group-safe one-feature decision stump
reaches 0.2659 Macro F1.

## Phase 8 entity challenge

`entity-challenge-v1` is a separate ownership-heuristic evaluation profile. It creates hidden
multi-address and singleton entities, hubs, address chains, same-entity observations across
different source IPs, different entities on shared infrastructure, strong and weak collaborative
transactions, and ordinary transactions with collaborative-looking shapes.

```bash
uv run python scripts/generate_scenarios.py \
  --profile entity-challenge-v1 --transactions 20000 --seed 42 \
  --output ../../benchmarks/generated/entity-challenge-v1-20000
```

`source.json` contains only Phase 1 input fields. `entity-truth.json` is evaluation-only and maps
opaque addresses to hidden entities and marks collaborative inputs. Each entity belongs wholly to
development, validation, or test; no entity crosses a tuning boundary. The audit rejects truth
keys in source, conflicting duplicate transaction definitions, duplicate truth membership, and
truth/source address-coverage differences. Exact source IP reuse across entities is intentional so
network identity cannot become an ownership shortcut.
