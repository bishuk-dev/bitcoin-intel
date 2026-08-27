# SIH 26146 — Bitcoin Intelligence Platform

AI-Powered Monitoring & Analysis of Bitcoin Transaction Traffic for SIH Problem Statement 26146.
The intended product is an offline Linux-based investigative intelligence platform.

## Current Status

**Phase 1 — Canonical Data Contract and Multi-Format Ingestion**

The repository contains the Phase 0 FastAPI health service and React developer screen plus a backend
CLI that validates CSV, JSON, or XML Bitcoin metadata and writes a deterministic seven-table Parquet
dataset. The authoritative schemas and source contracts are in
[`docs/data-contract.md`](docs/data-contract.md).

DuckDB, graph storage, analytics, machine learning, risk scoring, alerts, GeoIP enrichment, API
uploads, and investigation workflows are **not implemented yet**.

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
docs/architecture/     Implemented architecture decisions
infrastructure/docker/ Application Dockerfiles
```
