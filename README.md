# SIH 26146 — Bitcoin Intelligence Platform

AI-Powered Monitoring & Analysis of Bitcoin Transaction Traffic for SIH Problem Statement 26146.
The intended product is an offline Linux-based investigative intelligence platform.

## Current Status

**Phase 4 — Cross-Layer Correlation and Feature Engineering Foundation**

The repository contains the Phase 0 FastAPI health service and React developer screen, Phase 1
canonical ingestion, local DuckDB analytics, a derived Neo4j Community factual graph, and a
versioned deterministic Parquet feature layer. Canonical Parquet remains the durable source of
truth; DuckDB, Neo4j, and feature outputs are rebuildable from it.

The graph preserves transactions, addresses, IP endpoints, and coherent network-observation events
without claiming addresses are wallets or entities. Its CLI exposes bounded parameterized queries,
not arbitrary Cypher. See [`docs/graph-model.md`](docs/graph-model.md), the
[`offline deployment guide`](docs/deployment/neo4j-offline.md), and the
[`Phase 3 benchmark`](docs/benchmarks/phase-3.md).

Features measure transaction structure, address activity, network observations, temporal behaviour,
factual bipartite topology, and observational address/IP correlations. Snapshot and UTC
network-observation cutoff modes, definition-level lineage, atomic publication, validation, and a
truth-isolated connected scenario generator are documented in [`docs/features.md`](docs/features.md)
and [`docs/synthetic-scenarios.md`](docs/synthetic-scenarios.md).

Entity resolution, GeoIP enrichment, machine learning, risk scoring, alerts, graph HTTP endpoints,
and graph visualization are **not implemented yet**.

The detailed product constraints remain in `project-context.md` and `instructions.md`.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 24 LTS with npm 11 or 12
- Docker and Docker Compose (for the container workflow)
- GNU Make (optional convenience commands under Linux/WSL)

Dependency installation requires connectivity during development. Installed environments and built
containers do not use remote runtime dependencies.

## Backend Setup

From the repository root:

```bash
cd apps/backend
uv sync --group dev
uv run bitcoin-intel-api
```

The API listens on `http://127.0.0.1:8000` by default. Verify it with:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Ingest a supported source file into a new dataset directory with:

```bash
cd apps/backend
uv run bitcoin-intel ingest \
  --input tests/fixtures/equivalent/records.json \
  --output ./dataset
```

The destination must not already exist. The CLI reports read, accepted, rejected, transaction, and
observation counts; rejected source records remain queryable in the output dataset.

Validate and query a generated dataset through embedded DuckDB with:

```bash
uv run bitcoin-intel analytics validate --dataset ./dataset
uv run bitcoin-intel analytics tx --dataset ./dataset --txid <64-hex-txid>
uv run bitcoin-intel analytics address --dataset ./dataset --address <address>
uv run bitcoin-intel analytics temporal --dataset ./dataset --bucket day
```

Configuration is loaded from environment variables. For local CLI execution, copy `.env.example`
to `apps/backend/.env`; for Docker Compose, copy it to the repository-root `.env`. Never commit
either file.

Prepare and validate a derived graph import without running Neo4j:

```bash
uv run bitcoin-intel graph prepare --dataset ./dataset --output ./graph-import
uv run bitcoin-intel graph validate-import --input ./graph-import --dataset ./dataset
```

To import it into the Dockerized database, first set `NEO4J_PASSWORD` in `.env`, then use the
explicit destructive command described in [`docs/deployment/neo4j-offline.md`](docs/deployment/neo4j-offline.md).
Runtime checks and foundational queries include:

```bash
uv run bitcoin-intel graph health
uv run bitcoin-intel graph validate --dataset ./dataset
uv run bitcoin-intel graph tx --txid <64-hex-txid>
uv run bitcoin-intel graph gds-verify
```

Build and validate deterministic feature Parquet with:

```bash
uv run bitcoin-intel features build --dataset ./dataset --output ./features
uv run bitcoin-intel features build \
  --dataset ./dataset --output ./features-at-t --cutoff 2026-01-01T12:00:00Z
uv run bitcoin-intel features validate --features ./features --dataset ./dataset
```

## Frontend Setup

In a second terminal, from the repository root:

```bash
cd apps/frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`.

## Verification

Backend:

```bash
cd apps/backend
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest
```

An optional non-CI synthetic JSON ingestion sanity check is available with:

```bash
cd apps/backend
uv run python scripts/benchmark_ingestion.py --records 10000
```

The reproducible Phase 2 analytical benchmark remains manual because it can consume substantial
time and memory:

```bash
uv run python scripts/benchmark_phase2.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-2-local.json
```

The Phase 3 benchmark is also manual and replaces only its explicitly named isolated test database:

```bash
uv run python scripts/benchmark_phase3.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-3-local.json \
  --work-directory ../../benchmarks/work/phase3-local
```

The Phase 4 benchmark measures feature workers independently of canonical preparation:

```bash
uv run python scripts/benchmark_phase4.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-4-local.json \
  --work-directory ../../benchmarks/work/phase4-local
```

Frontend:

```bash
cd apps/frontend
npm run format:check
npm run lint
npm test
npm run build
```

Docker Compose configuration:

```bash
docker compose config
```

On Linux or WSL, after installing both projects, all non-interactive checks can be run with:

```bash
make verify
```

## Running with Docker Compose

After dependency lockfiles are present and a nonblank `NEO4J_PASSWORD` is configured, build and
start all services with:

```bash
docker compose up --build
```

The backend is available at `http://127.0.0.1:8000/health`, the frontend at
`http://127.0.0.1:5173`, Neo4j Browser at `http://127.0.0.1:7474`, and Bolt at
`neo4j://127.0.0.1:7687`. Stop containers with `docker compose down`; named Neo4j data/log volumes
remain unless explicitly removed.

## Repository Layout

```text
apps/backend/          FastAPI application and backend tests
apps/frontend/         React application and frontend tests
docs/data-contract.md  Authoritative Phase 1 source and canonical schemas
docs/analytics.md      Phase 2 analytical contract and CLI semantics
docs/graph-model.md    Phase 3 factual graph ontology and integrity rules
docs/features.md       Phase 4 feature schemas, lineage, time, null, and correlation semantics
docs/synthetic-scenarios.md  Connected evaluation-data generator and truth separation
docs/deployment/       Offline Neo4j image and operational workflow
docs/benchmarks/       Reproducible benchmark reports
docs/architecture/     Implemented architecture decisions
infrastructure/docker/ Application and pinned Neo4j Dockerfiles
```
