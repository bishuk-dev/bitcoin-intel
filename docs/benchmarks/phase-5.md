# Phase 5 ML Benchmark

The reproducible machine-readable result is
[`benchmarks/results/phase-5.json`](../../benchmarks/results/phase-5.json). Measurements were taken
on Windows 10 with four logical CPUs, Python 3.13.9, NumPy 2.5.2, PyArrow 23.0.1,
scikit-learn 1.9.0, and joblib 1.5.3.

## Method

A seed-42 bundle produced 10,000 transaction entities and 17,984 accepted observations. Scenario
generation, canonical ingestion, feature construction, and validation ran once and are excluded
from ML timings. Each configuration then ran in a fresh process with a deterministic group-aware
70/15/15 split. Training time covers the complete scikit-learn pipeline fit; inference time scores
or classifies all 10,000 rows. Peak RSS is the worker process lifetime peak and therefore includes
Python, Arrow, NumPy, scikit-learn, feature loading, training, and artifact writing. Artifact size is
the complete published experiment directory.

| Model | Feature family | Features | Train | Peak RSS | Inference | Artifacts |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Isolation Forest | all-eligible | 30 | 6.402 s | 230,981,632 B (220.28 MiB) | 0.421 s | 2,081,830 B (1.99 MiB) |
| Local Outlier Factor | all-eligible | 30 | 2.039 s | 231,256,064 B (220.54 MiB) | 0.674 s | 2,898,448 B (2.76 MiB) |
| Logistic Regression | all-eligible | 30 | 0.302 s | 230,715,392 B (220.03 MiB) | 0.083 s | 1,565,977 B (1.49 MiB) |
| Random Forest | all-eligible | 30 | 14.433 s | 231,018,496 B (220.32 MiB) | 0.935 s | 1,233,589 B (1.18 MiB) |
| Logistic Regression | transaction-only | 14 | 0.872 s | 227,708,928 B (217.16 MiB) | 0.091 s | 1,558,944 B (1.49 MiB) |
| Logistic Regression | network-only | 16 | 0.419 s | 227,053,568 B (216.53 MiB) | 0.074 s | 1,113,948 B (1.06 MiB) |

Peak RSS differs little because interpreter and numerical-library loading dominate this small
tabular workload. Random Forest has the highest training time; all inference paths remain below one
second except Random Forest, which remains near one second. LOF's larger artifact stores its fitted
neighbour reference data.

The combined 100,000-row suite was not run in Phase 5. LOF neighbour search has materially
different scaling from the other baselines, and repeating every ablation at 100k would mix model
scalability work with this framework-validation phase. The runner accepts additional transaction
counts so models can be profiled individually before a larger controlled comparison.

## Reproduction

```bash
cd apps/backend
uv run python scripts/benchmark_phase5.py \
  --transactions 10000 \
  --seed 42 \
  --output ../../benchmarks/results/phase-5-local.json
```

The output must be new unless `--replace-results` is explicitly supplied. Temporary generated data
is removed by default; `--work-directory` plus `--keep-data` retains it for manual artifact review.
