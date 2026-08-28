# Implemented Architecture Through Phase 4

The repository is a monorepo with independently managed backend and frontend applications:

- `apps/backend` is a Python 3.13 FastAPI project using a `src` package layout and `uv`.
- `apps/frontend` is a React and strict TypeScript application built with Vite and npm.
- `infrastructure/docker` contains the two application image definitions used by Docker Compose.

The backend now has one offline canonical ingestion path:

```text
CSV / JSON / XML
        ↓
streaming format adapters
        ↓
shared raw-record mapping
        ↓
Pydantic validation and normalization
        ↓
deterministic transaction deduplication and provenance
        ↓
explicit Parquet schemas with read-back verification
```

Blockchain transaction identity is stored separately from network observations. This prevents a
transaction from being duplicated when multiple source observations describe it. A transaction-to-
source bridge and deterministic record/observation IDs preserve provenance.

The Parquet writer stages all seven tables beside the requested destination, verifies schemas,
counts, keys, and relationships by reading them back, and then publishes with a same-filesystem
rename. It fails if the destination exists. The exact contracts are defined in
[`../data-contract.md`](../data-contract.md).

Phase 2 adds an embedded, scoped DuckDB query layer without changing that ownership model:

```text
CSV / JSON / XML
        ↓
Phase 1 ingestion
        ↓
Canonical Parquet (durable source of truth)
        ↓
manifest/schema validation
        ↓
in-memory DuckDB views + transaction_summary
        ↓
typed analytical operations and integrity checks
```

Each analytical session validates schema version `1.0.0`, resolves all canonical files within the
dataset root, registers seven direct-Parquet views, creates the derived `transaction_summary` view,
and closes its in-memory connection at the end of the context. There is no DuckDB server. The
benchmark-only materialized database is temporary and rebuildable from Parquet.

Phase 3 adds a separately operated factual graph projection:

```text
                       Canonical Parquet
                      /                 \
                     /                   \
        in-memory DuckDB views       graph-import builder
                                           ↓
                                  typed import Parquet
                                           ↓
                             Neo4j Community (derived)
                                           ↓
                              ephemeral GDS projections
```

The graph contains only `Transaction`, `Address`, `IPAddress`, and `NetworkObservation` nodes and
the five factual relationship types defined in [`../graph-model.md`](../graph-model.md). The event
node preserves source/destination roles, ports, time, TXID, and provenance as one observation.
`neo4j-admin database import full` replaces the graph only through an explicit confirmation-gated
CLI command, then creates Community-compatible uniqueness constraints and validates the live graph
against Parquet. Neo4j internal IDs have no external meaning.

The custom Neo4j image pins Community, GDS, and APOC, copies bundled plugin artifacts during the
connected build, and retains its plugin/offline configuration when exported with `docker save`.
Credentials and database state remain runtime configuration and named volumes. Host graph ports are
loopback-only.

The `transaction_summary` view aggregates inputs, outputs, and observations independently before
joining them by TXID. This prevents one-to-many relations from multiplying counts and satoshi sums.
All query values are parameterized; the only dynamic SQL expression is an internal allowlist for
`hour` and `day` UTC buckets.

Ubuntu Linux is the production target; Windows with WSL2 is the primary development setup.
Container and application paths therefore avoid Windows-specific assumptions.

The product is offline-first. Development dependency resolution and the initial base-image pull need
connectivity, but built runtime artifacts do not fetch packages or contact cloud services. The
Neo4j image can be SHA-256-recorded, saved, transferred, loaded, and run on an internal-only Docker
network without downloading plugins.

Phase 4 adds a second rebuildable Parquet layer without changing canonical ownership:

```text
                    Canonical Parquet
                   /        |        \
            DuckDB views  Neo4j/GDS  factual graph contract
                   \        |        /
                    scoped Feature Engine
                     /       |       \
          transaction   address/IP   correlation
                     \       |       /
                       Feature Parquet
```

DuckDB performs independently grouped value, endpoint, temporal, and correlation aggregates.
igraph runs one ephemeral factual Address–Transaction WCC projection and emits component size, not
unstable component identity. The feature manifest binds canonical, graph, feature-definition, and
build-configuration versions. Snapshot and network-observation cutoff builds share the same schemas;
cutoff mode filters observations and transaction admission before every downstream calculation.

The connected scenario generator is separate from the infrastructure benchmark generator. Its
evaluation truth sidecar is never passed into canonical ingestion, graph preparation, or features.
See [`../features.md`](../features.md) and [`../synthetic-scenarios.md`](../synthetic-scenarios.md).

GeoIP enrichment, entity resolution, machine learning, anomaly/risk scoring, alerts, graph HTTP
endpoints, and dashboard visualization remain future-phase concerns and have no placeholders.
