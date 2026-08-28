# Phase 4 Feature Contract

Phase 4 materializes deterministic measurements as derived Parquet. Canonical Parquet remains the
source of truth; feature output can be deleted and rebuilt. Features are not risk, guilt, ownership,
entity, or suspiciousness labels.

## Layout and identity

Each feature store contains:

```text
feature-manifest.json
feature-definitions.json
transaction_features/part-00000.parquet
address_features/part-00000.parquet
ip_features/part-00000.parquet
correlation_features/part-00000.parquet
```

`feature_schema_version` is `1.0.0` and evolves independently from canonical schema `1.0.0` and
graph schema `1.0.0`. The manifest records the canonical manifest SHA-256, all three schema
versions, calculation and definition versions, normalized build configuration, table rows, byte
sizes, and file hashes. `feature_dataset_id` hashes only semantic identity; operational `built_at`
does not affect it.

The emitted definition registry has one record per Parquet column with its table, entity type,
Arrow-equivalent dtype, description, canonical source tables, calculation version, temporal
semantics, nullability, and unit. Validation requires that registry to match the implementation.

## Tables

| Table | Key | Feature columns | Purpose |
| --- | --- | ---: | --- |
| `transaction_features` | `txid` | 35 | Value structure, endpoint diversity, observation time, entropy, and burst windows |
| `address_features` | `address` | 26 | Role occurrences, transaction/value activity, co-transaction adjacency, time, and WCC size |
| `ip_features` | `ip` | 24 | Endpoint roles, transactions, ports, reported metadata, time, entropy, and burst windows |
| `correlation_features` | `address` | 11 | Address/network associations through transactions and explicit IP-reuse measurements |

Transaction `input_count` and `output_count` are the unambiguous fan-in and fan-out measurements;
duplicate `fan_in`/`fan_out` aliases are intentionally omitted. Integer counts and satoshi sums use
`int64`. Monetary means, sample standard deviations, and ratios use `float64`; canonical monetary
columns are never changed or overwritten.

## Calculation semantics

Inputs, outputs, and observations are aggregated independently before joining. This prevents a
transaction with 3 inputs, 4 outputs, and 5 observations from producing multiplied counts or
satoshi sums.

- `input_value_std` and `output_value_std` use sample standard deviation and are `NULL` with fewer
  than two values.
- Ratios are `NULL` when their denominator is zero. A defined zero ratio remains `0.0`.
- Inter-observation values order by `(observed_at, observation_id)` and are `NULL` with fewer than
  two observations. Equal timestamps produce a valid zero-second interval.
- Entropy is natural-log Shannon entropy over UTC hour-of-day or UTC calendar-day buckets.
- Burst fields are the maximum row count in inclusive trailing 1-minute, 5-minute, and 1-hour
  timestamp windows.
- An IP occurring as both source and destination in one observation counts once in
  `total_observation_count`, once in each applicable role count, and its distinct ports are unioned.
- `co_transaction_address_count` counts distinct other addresses sharing a transaction. It excludes
  self-appearance and does not mean an input address directly paid an output address.
- A reused associated IP means an IP linked through at least
  `reused_ip_min_transactions` distinct containing transactions; the default is 2.

Undefined values are `NULL`, never sentinel numbers. Validation rejects negative constrained
measurements and every non-finite float (`NaN`, positive infinity, or negative infinity).

## Temporal modes and leakage boundary

Snapshot mode admits every canonical transaction and all observations. Its resulting full-history
aggregates are suitable for descriptive investigation and offline exploration, but must be treated
as snapshot-only in historical model evaluation.

Cutoff mode uses a UTC `--cutoff` and enforces both rules below:

1. admit only transactions with at least one network observation at or before the cutoff;
2. include only observations whose `observed_at <= cutoff`.

Inputs and outputs are immutable canonical facts for an admitted transaction. Address sets,
correlations, endpoint reuse, temporal values, and the graph projection are derived only from that
admitted scope. These calculations are therefore cutoff-safe relative to **network observation
time**. The source does not contain Bitcoin block confirmation time, so `first_observed_at` and
`last_observed_at` must never be described as block, confirmation, or true transaction time.

| Family | Snapshot build | Cutoff build |
| --- | --- | --- |
| Transaction structure/value | Full-history admitted scope | Safe after first network observation; not a block-time claim |
| Transaction/address/IP temporal | Snapshot-only | Cutoff-safe by filtered observations |
| Address/IP correlation | Snapshot-only | Cutoff-safe by filtered observations and admitted TXIDs |
| Bipartite WCC size | Snapshot-only | Cutoff-safe because the projection itself is cutoff-filtered |

## Graph projection

The topology feature uses an ephemeral, unweighted, undirected projection of factual
`Address`–`Transaction` edges corresponding to `SPENT_IN` and `CREATED_OUTPUT`. DuckDB assigns
deterministic disjoint integer vertex IDs and igraph runs native WCC. The projection is never
persisted and does not mutate Neo4j. Only `bipartite_component_size` is stored; implementation-
specific component IDs are discarded.

Component size counts both Address and Transaction vertices. Sharing a component does not imply
common control, common ownership, or one entity.

## Definition-level lineage

| Family | Canonical inputs | Intermediate relation | Calculation |
| --- | --- | --- | --- |
| Transaction | transactions, inputs, outputs, observations | independently grouped per TXID | SQL aggregates and timestamp windows |
| Address | inputs, outputs, observations | distinct `(address, txid)` then distinct observation association | SQL role/value/time aggregates and self-excluding co-transaction join |
| IP | observations | one deduplicated `(ip, observation_id)` row with role flags | SQL endpoint/port/time aggregates |
| Correlation | inputs, outputs, observations | distinct address/TXID and address/IP/TXID | SQL diversity and explicit configurable reuse threshold |
| Graph | scoped inputs and outputs | ephemeral factual bipartite projection | igraph weakly connected component sizes |

Lineage is deliberately definition-level. Row-level scalar lineage would substantially duplicate
canonical data without improving the reproducibility boundary.

## CLI and publication

```bash
uv run bitcoin-intel features build --dataset ./dataset --output ./features
uv run bitcoin-intel features build \
  --dataset ./dataset --output ./features-at-t \
  --cutoff 2026-01-01T12:00:00Z
uv run bitcoin-intel features validate --features ./features --dataset ./dataset
```

A build first validates canonical integrity, writes into a sibling temporary directory, validates
schemas, lineage, hashes, identities, constraints, references, and cutoff bounds, then atomically
renames the directory. Existing output is never silently overwritten. No command accepts arbitrary
SQL.

## Scientific caveats

An address is not a wallet, person, organization, or owner. An IP observed on a transaction does
not identify its originator or establish ownership of an address. Repeated IPs, high values,
fan-out, WCC size, and burst activity are descriptive measurements only. Phase 4 creates no entity
resolution, labels, scores, alerts, or learned models.
