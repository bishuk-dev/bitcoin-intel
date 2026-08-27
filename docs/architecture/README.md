# Implemented Architecture Through Phase 1

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

Ubuntu Linux is the production target; Windows with WSL2 is the primary development setup.
Container and application paths therefore avoid Windows-specific assumptions.

The product is offline-first. Development dependency resolution needs connectivity, but built runtime
artifacts must not fetch packages or contact cloud services. Packaging Docker images for transfer to an
air-gapped host is intentionally deferred until the deployment phase.

DuckDB query serving, graph storage, GeoIP enrichment, machine learning, risk scoring, investigation
APIs, and dashboard workflows remain future-phase concerns and have no placeholder implementations.
