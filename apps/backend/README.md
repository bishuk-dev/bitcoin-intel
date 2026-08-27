# Backend

The backend provides the Phase 0 FastAPI health endpoint:

```text
GET /health
```

From this directory, install and run it with:

```bash
uv sync --group dev
uv run bitcoin-intel-api
```

Phase 1 adds a separate offline ingestion CLI; ingestion is intentionally not exposed through
FastAPI:

```bash
uv run bitcoin-intel ingest \
  --input tests/fixtures/equivalent/records.json \
  --output ./dataset
```

The input extension must be `.csv`, `.json`, or `.xml`, and the output directory must not already
exist. The writer creates seven explicit, Zstandard-compressed Parquet tables plus `manifest.json`,
reads them back for verification, and only then publishes the directory. See
[`../../docs/data-contract.md`](../../docs/data-contract.md) for the complete source and canonical
contract.

Verification commands:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest
```

The application reads its settings from environment variables. Copy the repository root
`.env.example` to `.env` here when local overrides are needed; never commit `.env`.
