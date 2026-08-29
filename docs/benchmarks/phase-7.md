# Phase 7 Challenge ML Benchmark

This benchmark measures only Phase 7 challenge generation, classical model comparison, feature
ablation, calibration, stability, and selection. It does not repeat the DuckDB, Neo4j, or MMDB
throughput benchmarks.

## Command and environment

```bash
cd apps/backend
uv run python scripts/benchmark_phase7.py \
  --transactions 20000 \
  --output ../../benchmarks/results/phase-7.json \
  --selection-output ../../benchmarks/results/model-selection.json \
  --work-directory ../../benchmarks/work/phase-7-primary \
  --keep-data
```

Measured on Windows 10 19045 with 4 logical CPUs, Python 3.13.9, NumPy 2.5.2, SciPy 1.18.1,
scikit-learn 1.9.0, XGBoost CPU 3.4.1, and LightGBM 4.7.0. All model parameters and library versions
are recorded in experiment manifests. Native estimators are explicitly limited to four threads;
no GPU execution is enabled.

## Dataset

| Measure | Value |
| --- | ---: |
| Transactions | 20,000 |
| Network observations | 79,554 |
| Leakage-safe groups | 1,250 |
| Baseline | 8,035 |
| High fan-out | 2,938 |
| High value | 3,006 |
| Rapid sequence | 3,050 |
| Shared network | 2,971 |
| Weak / medium / strong injected patterns | 4,755 / 4,862 / 2,348 |
| Feature v2 model columns | 36 |

The controlled country/ASN fixture covers eight overlapping regions. Exact endpoints remain
group-local, and region identities appear across scenario classes.

## Model performance and cost

| Model | Macro F1 | Balanced accuracy | Train | Inference (20k) | Peak process RSS | Model | Experiment directory |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic Regression | 0.4875 | 0.5300 | 3.489 s | 0.092 s | 1.19 GiB | 3.6 KiB | 2.96 MiB |
| Random Forest | 0.4821 | 0.5150 | 9.426 s | 1.406 s | 1.19 GiB | 17.57 MiB | 20.53 MiB |
| HistGradientBoosting | 0.4666 | 0.4831 | 6.076 s | 1.232 s | 1.19 GiB | 483.0 KiB | 3.42 MiB |
| XGBoost | 0.4687 | 0.4811 | 13.790 s | 1.246 s | 1.19 GiB | 1.59 MiB | 4.54 MiB |
| LightGBM | 0.4611 | 0.4731 | 9.291 s | 4.651 s | 1.19 GiB | 2.12 MiB | 5.08 MiB |

Peak RSS is the process-lifetime peak across a shared benchmark process, so it is a conservative
common upper bound rather than incremental per-model memory. Training and inference timers are
per experiment. Python allocation tracing covers feature loading only and is disabled before
native model training so it does not distort timings.

## Search and stability

All five defaults were screened on validation. The two competitive models each received ten
deterministic trials. Logistic Regression selected `C=4.0`; Random Forest selected 240 trees,
depth 12, and leaf size 2. Search metadata records `test_rows_seen=0`.

| Finalist | Validation Macro F1 mean | Std dev | Mean training time | Training-time std dev |
| --- | ---: | ---: | ---: | ---: |
| Logistic Regression | 0.4811 | 0.0059 | 3.451 s | 0.580 s |
| Random Forest | 0.4925 | 0.0082 | 9.494 s | 0.101 s |

The Phase 7 preferred classifier is Logistic Regression, the supervised fallback is Random Forest,
and the preferred anomaly model is Isolation Forest. See
[`../ml-model-selection.md`](../ml-model-selection.md) for the complete decision matrix,
per-scenario, intensity, calibration, anomaly-capture, and limitation analysis.

## Scaling decision

The optional 100k finalist run was not executed. The required 20k primary benchmark already
provided stable group comparisons and all mandatory ablations; repeating finalist training at
100k would add cost without resolving the more important synthetic-to-real domain limitation.

## Docker and offline verification

The Phase 6 backend image was 509,635,157 bytes (486.03 MiB). The final Phase 7 image is
529,437,928 bytes (504.91 MiB), an increase of 18.89 MiB or 3.89%. The first import check correctly
found that LightGBM's CPU wheel needs `libgomp.so.1`, which is absent from Debian slim. The final
Dockerfile installs only the 319 KiB GNU OpenMP runtime; it does not install a compiler, OpenCL,
CUDA, or GPU tooling.

With `--network none` and the image's default UID 10001, the final image imported scikit-learn
1.9.0, XGBoost CPU 3.4.1, and LightGBM 4.7.0; loaded the hash-verified selected model; loaded all
20,000 rows from Feature Schema v2; and produced three predictions. An installed-distribution and
native-library scan found zero CUDA, NVIDIA, cuDNN, or NCCL runtime packages/libraries.
