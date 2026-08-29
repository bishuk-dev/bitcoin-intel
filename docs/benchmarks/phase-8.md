# Phase 8 entity-resolution benchmark

## Scope and method

The primary run used `entity-challenge-v1` with 20,000 unique transactions, seed 42, 2,500 hidden
entities, 7,400 addresses, 1,000 collaborative transactions, and 22,858 network observations.
Entities—not rows—were assigned to deterministic development/validation/test partitions. Shared
IPs deliberately cross truth entities. Hidden entity and collaboration truth was used only by the
evaluation boundary.

Three detector configurations were built independently. Development metrics were diagnostic;
selection used validation pairwise precision first, lower collaborative false-merge rate second,
and pairwise F1 third. The test partition was opened once after configuration freeze. Canonical
ingestion and Feature v1 preparation were excluded from entity-build timing. Address features are
schema-identical in Feature v1 and v2, so this isolates entity work without repeating Phase 6
enrichment benchmarks.

Command:

```bash
cd apps/backend
uv run python scripts/benchmark_phase8.py \
  --transactions 20000 --seed 42 \
  --output ../../benchmarks/results/phase-8-local.json
```

Measured artifact: [`../../benchmarks/results/phase-8.json`](../../benchmarks/results/phase-8.json).

## Environment

- Windows 10 host, Python 3.13.9, 4 logical CPUs
- PyArrow 23.0.1
- scikit-learn 1.9.0
- igraph 1.0.0

## Validation-only selection

| Candidate | Inputs / outputs / equal / fraction | Build seconds | Precision | Recall | F1 | Collaborative false merge |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0 (selected) | 3 / 3 / 2 / 0.50 | 26.18 | 1.000 | 1.000 | 1.000 | 0.000 |
| 1 | 4 / 4 / 3 / 0.50 | 25.44 | 0.695 | 1.000 | 0.820 | 0.145 |
| 2 | 4 / 4 / 4 / 0.75 | 27.15 | 0.695 | 1.000 | 0.820 | 0.145 |

The stricter configurations miss the challenge's weaker three-party collaborative pattern. Under
the precision-first policy, candidate 0 is the defensible frozen choice. These thresholds are
generator-validated defaults, not universal Bitcoin protocol constants.

## Held-out test

| Baseline | Pair precision | Pair recall | Pair F1 | B-cubed F1 | ARI | AMI | Over-merge | Fragmentation | Collaborative false merge |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Raw MIH | 0.0571 | 0.9950 | 0.1079 | 0.3287 | 0.1052 | 0.5063 | 0.5106 | 0.0040 | 1.0000 |
| Collaborative suppression | 1.0000 | 0.9926 | 0.9963 | 0.9984 | 0.9963 | 0.9979 | 0.0000 | 0.0060 | 0.0000 |
| Final conservative | 1.0000 | 0.9926 | 0.9963 | 0.9984 | 0.9963 | 0.9979 | 0.0000 | 0.0060 | 0.0000 |

Suppression removes the benchmark's collaborative false merges at a small recall/fragmentation
cost. The final partition intentionally equals the suppression baseline: network and community
evidence add audit context but no unvalidated ownership merges.

## Artifact and reproducibility results

- selected candidate rows: 2,506 entities; 7,400 memberships; 65,109 evidence rows; 20,000
  transaction diagnostics; 7,400 rows in each community table;
- six Parquet files total 4,803,824 bytes;
- independent selected rebuild: 25.92 seconds;
- semantic entity dataset IDs matched;
- every Parquet SHA-256 matched.

The optional 100k run was not executed. Candidate generation is linear in stored MIH edges, but the
explicit all-pairs co-transaction projection and HDBSCAN stage have different scaling behavior; a
100k claim would need separate memory instrumentation rather than extrapolation.

## Claim limits

The results show correctness and repeatability on the versioned synthetic generator. They do not
establish real-world address ownership, person identity, device attribution, guilt, criminality,
or universal detector accuracy. Perfect precision here requires continued generator review and
independent real-data validation before operational use.
