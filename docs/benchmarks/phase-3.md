# Phase 3 Graph Benchmark

Measured 2026-08-28 on Windows 10 with a 4-logical-CPU Intel processor, approximately 12 GiB RAM,
Python 3.13.9, Neo4j Community 2026.07.1, GDS Community 2026.07.0, APOC Core 2026.07.1, and Neo4j
driver 6.2.0. These are single-workstation measurements, not universal performance claims. Raw data
is in [`../../benchmarks/results/phase-3.json`](../../benchmarks/results/phase-3.json).

## Method

`scripts/benchmark_phase3.py` uses the existing deterministic Phase 2 generator with seed 42, 20%
repeated observations, 1–3 inputs and outputs, and 25% IPv6. Each size is ingested into canonical
Parquet, projected into graph-import Parquet, strictly dry-run/imported into an isolated database,
validated against canonical data, and queried five times through one verified driver. Preparation
and `neo4j-admin` import durations exclude Neo4j server restart time. Peak memory is the maximum
Docker-reported container memory sampled every 500 ms across the isolated Compose project during
the complete rebuild.

## Import performance

| Canonical records | Transactions | Nodes | Relationships | Prepare | Dry run | Import | Import Parquet | Store | Peak container memory |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 8,000 | 55,571 | 62,075 | 3.184 s | 39.787 s | 52.201 s | 3.64 MiB | 16.66 MiB | 1.25 GiB |
| 100,000 | 80,000 | 546,408 | 619,938 | 12.817 s | 40.679 s | 63.326 s | 34.74 MiB | 187.27 MiB | 1.41 GiB |

The 100k graph contains 319,938 addresses, 46,470 IPs, and 100,000 observations. Its relationship
counts are 159,980 `SPENT_IN`, 159,958 `CREATED_OUTPUT`, and 100,000 each for
`OBSERVED_TRANSACTION`, `SOURCE_IP`, and `DESTINATION_IP`.

## Query performance

First call includes driver/server query planning and cold-cache effects; repeated is the median of
five later calls through the same driver.

| Dataset | Query | First | Repeated median |
|---:|---|---:|---:|
| 10k | TXID neighborhood | 3287.277 ms | 96.323 ms |
| 10k | Address transactions | 901.587 ms | 18.425 ms |
| 10k | IP observations | 1743.629 ms | 47.933 ms |
| 10k | Bounded path (depth 2) | 613.377 ms | 21.387 ms |
| 100k | TXID neighborhood | 2701.413 ms | 98.606 ms |
| 100k | Address transactions | 1123.521 ms | 38.897 ms |
| 100k | IP observations | 2392.100 ms | 276.292 ms |
| 100k | Bounded path (depth 2) | 577.023 ms | 21.453 ms |

## GDS verification

The projection includes only `Address` and `Transaction`, with natural-direction `SPENT_IN` and
`CREATED_OUTPUT`. Memory is estimated first; WCC uses Community-compatible concurrency 1 and writes
nothing back.

| Dataset | Projected nodes | Projected relationships | Estimated memory | Components | First end-to-end | Repeated end-to-end | Reported WCC compute |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10k | 40,075 | 32,075 | 5.30–5.43 MiB | 8,000 | 5432.417 ms | 441.483 ms | 86 ms |
| 100k | 399,938 | 319,938 | 57.39–58.52 MiB | 80,000 | 6646.075 ms | 1679.184 ms | 346 ms |

Each synthetic transaction uses unique benchmark address names, so one WCC per transaction is the
expected connectivity result; it is not an entity claim.

## Decisions and limits

No extra indexes were added. The four uniqueness constraints back every Phase 3 lookup key, and
benchmark queries begin at those identities before traversing relationships. A transactional
`UNWIND` importer comparison was omitted: this phase needs an offline initial construction path,
`neo4j-admin full import` is designed for that use case, and a production-quality second importer
would duplicate failure, typing, and ordering logic without serving a runtime write requirement.

The optional 500k run was omitted to stay within the 12 GiB workstation budget and completion time;
10k and 100k meet the mandatory benchmark sizes. Neo4j/GDS startup takes materially longer than the
small graph import on this machine and is deliberately excluded from the import column.
