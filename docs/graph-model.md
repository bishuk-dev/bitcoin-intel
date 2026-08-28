# Phase 3 Factual Graph Model

Graph schema version: `1.0.0`  
Canonical data schema version: `1.0.0`

Apache Parquet remains the durable source of truth. Neo4j is a disposable, rebuildable projection:

```text
Canonical Parquet -> derived graph-import Parquet -> neo4j-admin full import -> Neo4j
```

Deleting the graph loses no unique information. `graph-manifest.json` binds a derived build to the
canonical manifest SHA-256, records both schema versions and all row/file hashes, and treats its
build timestamp as operational metadata rather than graph identity. A graph-schema version changes
when label, relationship, identity, or required-property semantics change; physical import tuning
alone does not require a version change.

## Nodes

| Label | Canonical identity | Properties | Meaning |
|---|---|---|---|
| `Transaction` | `txid` | `txid`, `fee_sats`, nullable `script_type` | One canonical Bitcoin transaction definition. |
| `Address` | `address` | `address` | One distinct canonical address used by any input or output. |
| `IPAddress` | `ip` | `ip` | One distinct canonical IPv4 or IPv6 endpoint in any observation role. |
| `NetworkObservation` | `observation_id` | `observation_id`, `observed_at`, `src_port`, `dst_port`, nullable `reported_geo_country`, nullable `reported_asn`, `source_record_id` | One coherent observation event and its provenance reference. |

An `Address` is not a wallet, person, owner, or entity. Phase 3 performs no ownership or entity
inference. Reported ASN/country values stay on the observation because the canonical source does not
prove which endpoint they describe. Complete `SourceRecord` nodes are not duplicated; the
`source_record_id` resolves full provenance through canonical Parquet.

## Relationships

| Relationship | Direction | Properties | Factual meaning |
|---|---|---|---|
| `SPENT_IN` | `Address -> Transaction` | `input_index`, `amount_sats` | The canonical input row associates this address with this transaction input. |
| `CREATED_OUTPUT` | `Transaction -> Address` | `output_index`, `amount_sats` | The canonical transaction creates this indexed output for the address. |
| `OBSERVED_TRANSACTION` | `NetworkObservation -> Transaction` | none | This event observed the identified transaction. |
| `SOURCE_IP` | `NetworkObservation -> IPAddress` | none | The event's explicit source endpoint role. |
| `DESTINATION_IP` | `NetworkObservation -> IPAddress` | none | The event's explicit destination endpoint role. |

`SPENT_IN` is not proof of ownership. `CREATED_OUTPUT` deliberately avoids the stronger actor claim
implied by names such as `SENT_TO`. No inferred or derived relationships (`SAME_ENTITY`,
`CO_SPENT_WITH`, IP-to-address ownership, or similar) are persisted in the factual graph.

Money is always an integer number of satoshis. Imported timestamps are Neo4j zoned datetimes and
validation compares epoch seconds plus nanoseconds so equivalent UTC instants remain equal even if
one temporal value carries a named `UTC` zone and another carries offset `Z`. Null values remain
absent Neo4j properties; no sentinel strings or integers are introduced.

## Import and integrity

The graph builder creates nine explicitly typed, Zstandard-compressed Parquet files with separate
Neo4j ID groups for transactions, addresses, IPs, and observations. Address and IP unions use set
semantics, while observations remain one node per canonical observation even when multiple events
refer to one TXID. Files are ordered by canonical keys and published atomically only after schema,
hash, duplicate-ID, endpoint, cardinality, value, and provenance validation.

The destructive rebuild command performs a strict `neo4j-admin database import full` dry run and
import. Bad-row tolerance is zero; duplicate nodes and bad relationships are never skipped. Database
replacement is available only through `graph rebuild` with `--confirm-replace-database` and is never
performed during normal application startup.

Four named uniqueness constraints are created immediately after Community Edition starts:

- `transaction_txid_unique`
- `address_address_unique`
- `ip_address_ip_unique`
- `network_observation_id_unique`

Their backing indexes serve all Phase 3 lookup entry points. No redundant or speculative additional
indexes are created: foundational queries enter through one of these identities and then traverse
relationships. Time-range and provenance-key lookup patterns are not exposed in this phase.

## Queries and GDS

The CLI exposes only fixed, parameterized operations: transaction neighborhood, address uses, IP
observations, and shortest path. Shortest paths are connectivity-only and capped at depth 8. Neo4j
internal IDs are neither returned nor persisted.

GDS validation estimates and then creates an ephemeral native projection containing only `Address`
and `Transaction` nodes with natural-direction `SPENT_IN` and `CREATED_OUTPUT` relationships. It runs
read-only Weakly Connected Components with concurrency 1 and always drops the projection. Component
membership means factual graph connectivity only; it is not an entity cluster.
