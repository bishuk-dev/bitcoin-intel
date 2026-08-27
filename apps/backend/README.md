# Backend

Phase 0 provides a minimal FastAPI application with one endpoint:

```text
GET /health
```

From this directory, install and run it with:

```bash
uv sync --group dev
uv run bitcoin-intel-api
```

Verification commands:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

The application reads its settings from environment variables. Copy the repository root
`.env.example` to `.env` here when local overrides are needed; never commit `.env`.

