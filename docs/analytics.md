# DuckDB Analytical Layer

Phase 2 adds local SQL analytics over a completed Phase 1 dataset. Canonical Parquet remains the
durable source of truth. DuckDB is an embedded query engine: it is opened in memory for each scoped
session, not deployed as a server, and not used as the only durable copy of canonical data.

## Opening a dataset

`AnalyticalDataset(path)` validates the boundary before callers receive a connection:

- `manifest.json` must exist and be valid JSON;
- its schema version must be one of the explicitly supported versions (currently only `1.0.0`);
- its table set must exactly match the seven canonical tables;
- each declared `part-00000.parquet` must exist inside the dataset root; and
- each Parquet schema must match the Phase 1 Arrow schema.

The session registers direct-Parquet views named `transactions`, `transaction_inputs`,
`transaction_outputs`, `network_observations`, `transaction_sources`, `source_records`, and
`rejected_records`. Callers can therefore use stable logical names without handling file paths.
Connections are never global and are closed by the context manager.

## Derived view

`transaction_summary` provides one row per canonical transaction:

```text
txid
input_count
output_count
total_input_sats
total_output_sats
fee_sats
network_observation_count
first_observed_at
last_observed_at
```

Inputs, outputs, and observations are grouped separately and then joined to transactions. A direct
`inputs × outputs × observations` join would multiply rows and corrupt counts and sums. Empty child
relations produce zero counts/totals and null observation timestamps. DuckDB promotes integer sums
to `HUGEINT`; Python exposes them as exact integers even when an aggregate exceeds signed int64.

## Available operations

- **Transaction lookup** validates a 64-hex TXID and returns its transaction, inputs ordered by
  index, outputs ordered by index, observations ordered by time/ID, and source provenance. A missing
  TXID returns no result.
- **Address activity** counts distinct transactions and input/output appearances and sums each side
  independently. `first_observed_at` and `last_observed_at` are the earliest/latest network
  observations for matching TXIDs. They are not blockchain confirmation times and do not establish
  entity identity.
- **High value** ranks by `total_output_sats`, with fee and fan-in/fan-out fields available
  separately. It does not add inputs and outputs into a misleading volume figure.
- **High fee** ranks by canonical `fee_sats`. `fee_to_input_ratio` is a derived floating-point value
  and is null when total input is zero; canonical money remains integer satoshis.
- **IP activity** counts distinct observations and TXIDs, source/destination role occurrences,
  ports, reported ASNs/countries, and the observation time range. An observation is not proof that
  an IP owns a wallet or transaction.
- **ASN activity** uses only source-reported Phase 1 ASN values and returns observations, unique IPs,
  unique TXIDs, and the observed time range. There is no GeoIP enrichment.
- **Temporal activity** groups observations into `hour` or `day` buckets after setting DuckDB's
  session timezone to UTC. Optional range bounds are inclusive at the start and exclusive at the
  end.
- **Transaction summaries** expose the fan-in/fan-out baseline without assigning suspicion or risk.

All externally supplied values use DuckDB parameters. The temporal bucket SQL is selected from the
internal `hour`/`day` allowlist. Phase 2 provides no raw-SQL CLI or API surface.

## Integrity validation

`analytics validate` performs defense-in-depth checks over the registered Parquet views:

- orphan inputs, outputs, network-observation TXIDs/sources, transaction-source links, and rejected
  source-record links;
- duplicate transaction, input, output, observation, transaction-source, and source-record keys;
- negative input/output amounts, transaction fees, and input/output indexes; and
- actual table row counts that disagree with the manifest.

A valid report contains no issues. Invalid reports contain stable codes, affected counts, and
messages, and the CLI exits with status `2`. A dataset that cannot be opened fails before integrity
queries run.

## CLI

From `apps/backend`:

```bash
uv run bitcoin-intel analytics validate --dataset ./dataset
uv run bitcoin-intel analytics tx --dataset ./dataset --txid <64-hex-txid>
uv run bitcoin-intel analytics address --dataset ./dataset --address <address>
uv run bitcoin-intel analytics high-value --dataset ./dataset --limit 20
uv run bitcoin-intel analytics high-fee --dataset ./dataset --limit 20
uv run bitcoin-intel analytics ip --dataset ./dataset --ip 2001:db8::1
uv run bitcoin-intel analytics asn --dataset ./dataset --asn 64500
uv run bitcoin-intel analytics temporal --dataset ./dataset --bucket day \
  --start 2026-01-01T00:00:00Z --end 2026-02-01T00:00:00Z
```

Results are JSON. Limits must be positive, timestamps must include a timezone, IPs are normalized by
the standard library, and TXIDs are normalized to lowercase.

## Null and empty-data semantics

`script_type`, `reported_geo_country`, and `reported_asn` retain true nulls when unknown. The layer
does not invent values such as `UNKNOWN`, `0`, or `-1`. Missing observation ranges and a zero-input
fee ratio are also null. Empty valid datasets return empty ranked/temporal results and zeroed address
or IP summaries rather than failing.

## Scope and limitations

The Phase 1 accumulator remains O(n) in memory; Phase 2 measures this but does not rewrite ingestion.
There are no analytical HTTP endpoints, graph/entity resolution, GeoIP lookup, ML features, risk
labels, alerts, or frontend investigation screens. Physical-layout and optional materialization
findings are documented in [`benchmarks/phase-2.md`](benchmarks/phase-2.md).
