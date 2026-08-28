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

Phase 2 adds embedded DuckDB sessions over those Parquet files. Parquet remains canonical; DuckDB is
not a server and is not the sole durable store. Use the safe, typed analytical subcommands:

```bash
uv run bitcoin-intel analytics validate --dataset ./dataset
uv run bitcoin-intel analytics tx --dataset ./dataset --txid <64-hex-txid>
uv run bitcoin-intel analytics address --dataset ./dataset --address <address>
uv run bitcoin-intel analytics high-value --dataset ./dataset --limit 20
uv run bitcoin-intel analytics high-fee --dataset ./dataset --limit 20
uv run bitcoin-intel analytics ip --dataset ./dataset --ip 192.0.2.1
uv run bitcoin-intel analytics asn --dataset ./dataset --asn 64500
uv run bitcoin-intel analytics temporal --dataset ./dataset --bucket hour
```

No command accepts arbitrary SQL. Full query and null/timestamp semantics are documented in
[`../../docs/analytics.md`](../../docs/analytics.md).

Run the deterministic Phase 2 benchmark manually; it is deliberately excluded from pytest:

```bash
uv run python scripts/benchmark_phase2.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-2-local.json
```

Measured results and the physical-layout recommendation are in
[`../../docs/benchmarks/phase-2.md`](../../docs/benchmarks/phase-2.md).

Verification commands:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest
```

The application reads its settings from environment variables. Copy the repository root
`.env.example` to `.env` here when local overrides are needed; never commit `.env`.
