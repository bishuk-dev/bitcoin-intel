# Phase 4 Feature Benchmark

The reproducible benchmark result is
[`benchmarks/results/phase-4.json`](../../benchmarks/results/phase-4.json). It was measured on Windows
10, Python 3.13.9, DuckDB 1.5.5, PyArrow 23.0.1, and igraph 1.0.0 with four logical CPUs.

## Method

The existing deterministic Phase 2 workload configuration was used: seed 42, 20% duplicate network
observations, 1–3 inputs, 1–3 outputs, and 25% IPv6 selection. Canonical datasets were generated
once. Their generation and known O(n)-memory ingestion are explicitly excluded from feature timing
and RSS.

Each feature build ran in a new worker process. Wall time covers canonical integrity validation,
scoped DuckDB aggregation, WCC projection, all four Parquet writes, hashes, and read-back feature
validation. Worker current RSS was sampled every 20 ms. Output size includes Parquet, definitions,
and manifest files.

| Source records | Feature rows | Build time | Peak RSS | Output size |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 77,646 | 3.648 s | 199,233,536 B (190.00 MiB) | 2,356,936 B (2.25 MiB) |
| 100,000 | 766,346 | 23.780 s | 805,625,856 B (768.30 MiB) | 23,471,524 B (22.38 MiB) |

The 100k row breakdown was 80,000 transactions, 319,938 addresses, 46,470 IPs, and 319,938 address
correlation rows.

## Interpretation

Feature rows and output size scale close to linearly. The 100k build is 6.52 times slower for 9.87
times as many output rows because fixed startup/validation costs are proportionally larger at 10k.
Peak memory grows 4.04 times and is dominated by the ephemeral WCC address/transaction vertices and
edge list. The graph computation uses deterministic integer IDs and a native O(V + E) WCC; an
initial accidental O(n²) membership-materialization access was identified during benchmarking and
removed before these measurements.

No Python per-entity database calls, DataFrame concatenation loops, Neo4j N+1 queries, or graph
transitive closure are used. SQL scans remain table/family scoped. A future scale phase should
consider chunked or external-memory topology algorithms if the in-memory WCC projection becomes the
dominant constraint.

Phase 2's canonical accumulator remains known O(n) ingestion technical debt and was not redesigned
as part of Phase 4.

## Reproduction

```bash
cd apps/backend
uv run python scripts/benchmark_phase4.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-4-local.json \
  --work-directory ../../benchmarks/work/phase4-local \
  --keep-data
```

Use `--reuse-canonical` only when the matching `records-<n>/dataset` directories already exist; this
option reruns feature workers without repeating canonical ingestion.
