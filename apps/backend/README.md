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

Phase 3 adds a derived Neo4j Community graph without changing Parquet ownership. Prepare and fully
validate import Parquet without requiring a running database:

```bash
uv run bitcoin-intel graph prepare --dataset ./dataset --output ./graph-import
uv run bitcoin-intel graph validate-import --input ./graph-import --dataset ./dataset
```

Database replacement is never automatic. The `graph rebuild` command performs a strict
`neo4j-admin` dry run/import only when `--confirm-replace-database` is present. After import, use:

```bash
uv run bitcoin-intel graph health
uv run bitcoin-intel graph validate --dataset ./dataset
uv run bitcoin-intel graph tx --txid <64-hex-txid>
uv run bitcoin-intel graph address --address <address>
uv run bitcoin-intel graph ip --ip 192.0.2.1
uv run bitcoin-intel graph path \
  --source-kind address --source-id <address> \
  --target-kind transaction --target-id <64-hex-txid> --max-depth 4
uv run bitcoin-intel graph gds-verify
```

These are fixed, parameterized, bounded operations; there is no arbitrary Cypher interface. See
[`../../docs/graph-model.md`](../../docs/graph-model.md) for semantics and
[`../../docs/deployment/neo4j-offline.md`](../../docs/deployment/neo4j-offline.md) for credentials,
the destructive rebuild workflow, and air-gapped image transfer.

Phase 6 adds derived offline country/ASN enrichment and Feature Schema v2. Stage licensed DB-IP
Lite MMDB resources locally; neither command downloads data:

```bash
uv run bitcoin-intel enrichment build \
  --dataset ./dataset \
  --country-db ../../resources/geoip/dbip-country-lite.mmdb \
  --asn-db ../../resources/geoip/dbip-asn-lite.mmdb \
  --output ./enrichment
uv run bitcoin-intel enrichment validate --dataset ./dataset --enrichment ./enrichment
uv run bitcoin-intel features build \
  --dataset ./dataset --enrichment ./enrichment --output ./features
uv run bitcoin-intel features build \
  --dataset ./dataset --enrichment ./enrichment \
  --output ./features-at-t --cutoff 2026-01-01T12:00:00Z
uv run bitcoin-intel features validate \
  --features ./features --dataset ./dataset --enrichment ./enrichment
```

Resource provenance, attribution, lookup nulls, and accuracy limitations are documented in
[`../../docs/ip-enrichment.md`](../../docs/ip-enrichment.md). Feature definitions, lineage, nulls,
graph projection, v1 compatibility, and v2 IP/address caveats are documented in
[`../../docs/features.md`](../../docs/features.md). Generate connected, truth-isolated evaluation data with `scripts/generate_scenarios.py`; see
[`../../docs/synthetic-scenarios.md`](../../docs/synthetic-scenarios.md).

Phase 5 adds local transaction-level ML experiments. Synthetic truth is accepted only by this
experiment/evaluation boundary and never enters feature Parquet:

```bash
uv run bitcoin-intel ml train-anomaly \
  --features ./features --truth ./scenario-truth.json \
  --model isolation-forest --split group --output ./experiments
uv run bitcoin-intel ml train-anomaly \
  --features ./features --truth ./scenario-truth.json \
  --model local-outlier-factor --split group --output ./experiments
uv run bitcoin-intel ml train-scenario \
  --features ./features --truth ./scenario-truth.json \
  --model logistic-regression --split group --output ./experiments
uv run bitcoin-intel ml train-scenario \
  --features ./features --truth ./scenario-truth.json \
  --model random-forest --split group --output ./experiments
uv run bitcoin-intel ml evaluate --experiment ./experiments/<experiment-id>
```

Feature families include `transaction-only`, `transaction-temporal`, `transaction-network`,
`transaction-network-enrichment`, `cross-layer`, `network-only`, and `all-eligible`. Phase 7 also
supports `hist-gradient-boosting`, `xgboost`, `lightgbm`, and `pca-reconstruction`; optional
`--calibration sigmoid` is training-only. Generated experiment
directories are ignored by Git. Treat joblib as trusted-local-only serialized code; the evaluation
command never loads it. Full methodology and claim limits are in
[`../../docs/ml-baselines.md`](../../docs/ml-baselines.md) and
[`../../docs/ml-model-selection.md`](../../docs/ml-model-selection.md).

The production image installs Debian `libgomp1`, the minimal OpenMP runtime required by the
LightGBM CPU wheel. It does not include CUDA or other GPU runtime packages.

Phase 8 builds a distinct, versioned entity-hypothesis store. Only unsuppressed multi-input
evidence can merge addresses; network evidence and HDBSCAN/Leiden communities cannot:

```bash
uv run bitcoin-intel entity build \
  --dataset ./dataset --features ./features --output ./entity-hypotheses
uv run bitcoin-intel entity validate \
  --entities ./entity-hypotheses --dataset ./dataset --features ./features
uv run bitcoin-intel entity evaluate \
  --entities ./entity-hypotheses --dataset ./dataset --features ./features \
  --truth ./entity-truth.json --partition test
```

See [`../../docs/entity-resolution.md`](../../docs/entity-resolution.md). Candidate entities,
membership support, network support, and communities are analytical hypotheses—not verified
wallets, owners, people, control, guilt, criminality, or risk.

Run the deterministic Phase 2 benchmark manually; it is deliberately excluded from pytest:

```bash
uv run python scripts/benchmark_phase2.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-2-local.json
```

Measured results and the physical-layout recommendation are in
[`../../docs/benchmarks/phase-2.md`](../../docs/benchmarks/phase-2.md).

Run the manual Phase 3 graph benchmark only with Docker and explicit test paths:

```bash
uv run python scripts/benchmark_phase3.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-3-local.json \
  --work-directory ../../benchmarks/work/phase3-local
```

Run the Phase 4 feature benchmark independently of canonical preparation:

```bash
uv run python scripts/benchmark_phase4.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-4-local.json \
  --work-directory ../../benchmarks/work/phase4-local
```

Run the Phase 5 ML and ablation benchmark with one fresh process per experiment:

```bash
uv run python scripts/benchmark_phase5.py \
  --transactions 10000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-5-local.json
```

Run the Phase 6 enrichment/Feature v2 benchmark without repeating older subsystem benchmarks:

```bash
uv run python scripts/benchmark_phase6.py \
  --records 10000 100000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-6-local.json \
  --work-directory ../../benchmarks/work/phase6-local
```

Run the Phase 7 challenge/model-selection benchmark. The retained work directory is ignored and
holds trusted local artifacts for offline verification:

```bash
uv run python scripts/benchmark_phase7.py \
  --transactions 20000 \
  --output ../../benchmarks/results/phase-7-local.json \
  --selection-output ../../benchmarks/results/model-selection-local.json \
  --work-directory ../../benchmarks/work/phase7-local --keep-data
```

Run the Phase 8 entity benchmark with validation-only detector selection and one held-out test:

```bash
uv run python scripts/benchmark_phase8.py \
  --transactions 20000 --seed 42 \
  --output ../../benchmarks/results/phase-8-local.json
```

Verification commands:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest
```

The application reads its settings from environment variables. Copy the repository root
`.env.example` to `.env` here when local overrides are needed; never commit `.env`.
