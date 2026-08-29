# Phase 6 Enrichment and Feature v2 Benchmark

This benchmark measures only Phase 6 work. It does not repeat compression, DuckDB layout, Neo4j
import, or Phase 5 training benchmarks. Canonical fixture generation and ingestion happen once per
size and are excluded from timed workers.

## Method

Command:

```bash
cd apps/backend
uv run python scripts/benchmark_phase6.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-6.json
```

Each enrichment and Feature v2 build runs in a fresh process. Current RSS is sampled every 20 ms.
The deterministic synthetic source has 20% repeated transaction definitions but 10k/100k distinct
network observations. The script generates purpose-built country/ASN MMDB resources that cover a
controlled subset of its documentation ranges. These fixtures test stable hit/miss behavior; they
do not claim real DB-IP coverage or accuracy.

Environment: Windows 10 19045, Python 3.13.9, 4 logical CPUs, DuckDB 1.5.5, PyArrow 23.0.1, and
maxminddb 3.1.1. Exact machine and raw measurements are preserved in
[`../../benchmarks/results/phase-6.json`](../../benchmarks/results/phase-6.json).

## Enrichment results

Lookup count is two per distinct IP: one country plus one ASN lookup.

| Observations | Distinct IPs | Total lookups | Time | Lookups/s | Peak RSS | Output |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 5,496 | 10,992 | 5.151 s | 2,133.87 | 101.7 MiB | 28.4 KiB |
| 100,000 | 46,470 | 92,940 | 38.459 s | 2,416.57 | 162.7 MiB | 216.0 KiB |

| Observations | Country hit/miss | ASN hit/miss | IPv4 / IPv6 |
| ---: | ---: | ---: | ---: |
| 10,000 | 4,200 / 1,296 | 4,200 / 1,296 | 508 / 4,988 |
| 100,000 | 26,059 / 20,411 | 26,059 / 20,411 | 508 / 45,962 |

The pipeline performs lookups per distinct endpoint rather than per observation. At 100k, 46,470
rows replace 200,000 raw endpoint occurrences, while country and ASN readers are each opened once.

## Feature Schema v2 results

| Observations | Feature rows | Time | Peak RSS | Output |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 77,646 | 9.159 s | 236.8 MiB | 2.29 MiB |
| 100,000 | 766,346 | 43.811 s | 1.04 GiB | 22.70 MiB |

Feature output contains transaction, address, IP, and correlation tables. At 100k, address and
correlation cardinality (319,938 rows each) dominates memory, not MMDB lookup. The measured 1.04
GiB peak is acceptable for the current local benchmark target but should remain a capacity-planning
constraint; larger datasets may require query/materialization profiling before claiming broader
scale.

## Interpretation

This benchmark demonstrates deterministic offline throughput and bounded distinct-IP work on the
existing representative generator. It is not an accuracy study, a provider comparison, or a model
quality benchmark. Country/ASN hit rates are properties of the controlled benchmark MMDB, not a
claim about DB-IP Lite production coverage.
