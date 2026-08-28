# Phase 2 Benchmark Report

These are local measurements, not universal performance claims. Machine-readable results are in
[`../../benchmarks/results/phase-2-100k.json`](../../benchmarks/results/phase-2-100k.json) and
[`../../benchmarks/results/phase-2-500k.json`](../../benchmarks/results/phase-2-500k.json).

## Environment

| Item | Measured value |
| --- | --- |
| Platform | Windows 10.0.19045 |
| CPU | Intel64 Family 6 Model 78, 4 logical CPUs |
| Physical memory | 12,744,126,464 bytes (11.87 GiB) |
| Python | 3.13.9 |
| DuckDB | 1.5.5 |
| Polars | 1.44.1 |
| PyArrow | 23.0.1 |

## Methodology

The generator emits deterministic, schema-valid JSON from seed `42`, with 1–3 inputs, 1–3 outputs,
25% IPv6 observations, and a 20% duplicate-observation ratio. Duplicate observations retain the
same canonical transaction definition. Generation and ingestion run in an isolated worker. Elapsed
time uses `time.perf_counter()`; RSS is sampled every 20 ms and covers generation plus ingestion.

Each direct-Parquet query's first measurement uses a fresh in-memory DuckDB connection. “Repeated”
is the median of five subsequent executions for 10k/100k and three for 500k in the same connection.
The OS filesystem cache was not controlled, so “first” is cold-ish rather than a guaranteed cold
read. Layout experiments use the 100k `network_observations` table and median five repeated reads.

## Ingestion scaling

| Records | Input size | Ingestion time | Records/sec | Peak RSS | Output size |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 5.09 MiB | 8.095 s | 1,235.39 | 170.19 MiB | 3.34 MiB |
| 100,000 | 50.94 MiB | 65.619 s | 1,523.94 | 604.38 MiB | 32.39 MiB |
| 500,000 | 254.64 MiB | 626.704 s | 797.82 | 2,382.29 MiB | 160.19 MiB |

Generation took 3.126 s, 23.763 s, and 218.230 s respectively and is not included in the ingestion
time column. All generated records were accepted. The 20% repeated observations yielded 8k, 80k,
and 400k unique transactions.

The result confirms the known O(n) Phase 1 accumulator cost: peak RSS grew from 170 MiB to 2.33 GiB,
and throughput fell at 500k. One million records was skipped because the 500k run already required
626.7 seconds and 2.50 GB measured RSS on a four-logical-CPU machine; doubling it was not necessary
to establish the limitation and risked disrupting the development system.

## Direct-Parquet query performance

Times are milliseconds.

| Dataset | Workload | First | Repeated median |
| ---: | --- | ---: | ---: |
| 10k | TXID lookup | 179.86 | 76.21 |
| 10k | Address activity | 35.33 | 35.80 |
| 10k | High-value ranking | 48.99 | 50.65 |
| 10k | UTC day aggregation | 26.35 | 27.29 |
| 10k | IP activity | 36.49 | 47.58 |
| 10k | `transaction_summary` scan | 35.30 | 32.77 |
| 100k | TXID lookup | 165.37 | 175.38 |
| 100k | Address activity | 62.27 | 75.91 |
| 100k | High-value ranking | 199.54 | 182.75 |
| 100k | UTC day aggregation | 140.45 | 124.00 |
| 100k | IP activity | 86.56 | 73.14 |
| 100k | `transaction_summary` scan | 140.10 | 138.37 |
| 500k | TXID lookup | 467.07 | 402.93 |
| 500k | Address activity | 234.07 | 247.53 |
| 500k | High-value ranking | 1,057.38 | 1,073.88 |
| 500k | UTC day aggregation | 533.85 | 555.68 |
| 500k | IP activity | 195.64 | 190.12 |
| 500k | `transaction_summary` scan | 1,006.69 | 934.99 |

DuckDB `EXPLAIN` showed `PARQUET_SCAN` operators with both filters and limited projections for the
TXID and timestamp-range probes. This verifies predicate pushdown and column pruning are present in
the plans; it does not claim that the OS cache was cold or quantify row-group elimination.

## Parquet layout experiments

All sizes below are for 100,000 `network_observations` rows. Read columns are repeated-run medians in
milliseconds. The selective query filters one TXID; the time query filters one UTC day.

### Row groups

| Rows/group | Groups | Size | Write | Full scan | TXID filter | Time filter |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 32,768 | 4 | 11.41 MiB | 407.23 ms | 30.63 | 29.62 | 27.17 |
| 65,536 (current) | 2 | 11.32 MiB | 381.01 ms | 48.12 | 32.83 | 35.50 |
| 131,072 | 1 | 11.24 MiB | 572.36 ms | 58.76 | 62.44 | 52.86 |
| 524,288 | 1 | 11.24 MiB | 336.42 ms | 63.60 | 55.43 | 35.75 |

The 32k variant was faster in this run, while 65k wrote faster and was slightly smaller. The two
largest settings collapsed to one row group at this dataset size and generally read more slowly.
One synthetic run is not strong enough evidence for a production writer change.

### Compression

| Compression | Size | Write | Full scan | TXID filter | Time filter |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ZSTD | 11.32 MiB | 381.01 ms | 48.12 | 32.83 | 35.50 |
| Snappy | 20.55 MiB | 283.65 ms | 37.72 | 25.16 | 28.41 |
| None | 22.63 MiB | 185.20 ms | 33.99 | 15.01 | 15.89 |

ZSTD used about 45% less space than Snappy and 50% less than uncompressed data, at higher local CPU
cost. Storage efficiency is the stronger trade-off for this offline analytical system, so ZSTD
remains the baseline.

### File count

| Files | Size | Write | Full scan | TXID filter | Time filter |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 11.32 MiB | 380.89 ms | 50.90 | 31.46 | 33.85 |
| 4 | 11.49 MiB | 394.33 ms | 29.12 | 26.06 | 19.85 |
| 32 | 11.70 MiB | 597.63 ms | 38.24 | 24.80 | 29.95 |

Four files improved these local reads, but at 100k rows each file is small and the experiment does
not establish a durable split threshold. Thirty-two files increased write time by 57% over one file
and did not improve the broader scans over four files. No high-cardinality or time partitioning was
tested: current table sizes and sub-second 500k selective/temporal queries do not justify the added
layout and tiny-file complexity.

## Rebuildable DuckDB materialization

Materializing all seven 100k-run tables took 2.806 seconds and produced a 42.26 MiB database versus
32.39 MiB for the canonical dataset (about 30% extra). No indexes were created.

| Workload | Direct first/repeated | Materialized first/repeated |
| --- | ---: | ---: |
| TXID lookup | 165.37 / 175.38 ms | 129.87 / 59.07 ms |
| Address activity | 62.27 / 75.91 ms | 32.91 / 33.60 ms |
| High-value ranking | 199.54 / 182.75 ms | 180.62 / 144.30 ms |
| UTC day aggregation | 140.45 / 124.00 ms | 178.96 / 177.50 ms |
| IP activity | 86.56 / 73.14 ms | 60.71 / 35.98 ms |
| `transaction_summary` scan | 140.10 / 138.37 ms | 133.80 / 136.22 ms |

Materialization helped point lookups but was neutral or slower for some scans. It therefore remains
an optional, rebuildable future cache experiment rather than the default or canonical store.

## Recommendation

Retain the Phase 1 physical layout for now:

- ZSTD compression with dictionary encoding and statistics;
- 65,536-row groups;
- one predictable Parquet part per canonical table at the measured scale;
- no partitioning; and
- direct Parquet DuckDB views by default, with no retained indexes or durable DuckDB requirement.

The 32k row-group and four-file variants deserve retesting on larger, production-shaped datasets,
but this single synthetic benchmark does not demonstrate enough consistent benefit to change an
already deterministic and atomic writer. Revisit moderate file splitting only when real table sizes
make one-file scans or writes a measured bottleneck. Never partition by TXID, address, or IP.

## Limitations

- Synthetic values exercise the schema and relationships, not real Bitcoin traffic distribution.
- Results are from one Windows machine and one benchmark pass; scheduler and filesystem cache noise
  remain.
- Layout variants cover only `network_observations` at 100k rows.
- The 500k run covers ingestion and direct-Parquet queries; layout and materialization experiments
  were intentionally not repeated at that cost.
- RSS sampling is process-level and includes generation plus ingestion, not allocation attribution.
