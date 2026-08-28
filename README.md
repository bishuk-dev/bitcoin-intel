# SIH 26146 — Bitcoin Intelligence Platform

AI-Powered Monitoring & Analysis of Bitcoin Transaction Traffic for SIH Problem Statement 26146.
The intended product is an offline Linux-based investigative intelligence platform.

## Current Status

**Phase 2 — Embedded DuckDB Analytical Layer**

The repository contains the Phase 0 FastAPI health service and React developer screen, the Phase 1
canonical ingestion pipeline, and a local DuckDB analytical layer over the seven-table Parquet
dataset. Parquet remains the durable source of truth; every analytical session and experimental
DuckDB materialization is rebuildable from it.

The analytical CLI provides typed transaction, address, network, temporal, value, fee, and integrity
operations without exposing arbitrary SQL. See [`docs/analytics.md`](docs/analytics.md) for query
semantics and [`docs/benchmarks/phase-2.md`](docs/benchmarks/phase-2.md) for measured performance and
the retained physical-layout decision.

Graph storage, machine learning, risk scoring, alerts, GeoIP enrichment, API uploads, and
investigation workflows are **not implemented yet**.

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

Configuration is loaded from environment variables. Copy `.env.example` into `apps/backend/.env`
only when local overrides are needed. Never commit `.env`.

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

After dependency lockfiles are present, build and start both services with:

```bash
docker compose up --build
```

The backend is available at `http://127.0.0.1:8000/health` and the frontend at
`http://127.0.0.1:5173`. Stop them with `docker compose down`.

## Repository Layout

```text
apps/backend/          FastAPI application and backend tests
apps/frontend/         React application and frontend tests
docs/data-contract.md  Authoritative Phase 1 source and canonical schemas
docs/analytics.md      Phase 2 analytical contract and CLI semantics
docs/benchmarks/       Reproducible benchmark reports
docs/architecture/     Implemented architecture decisions
infrastructure/docker/ Application Dockerfiles
```
