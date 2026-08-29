# Phase 5 ML Baselines

Phase 5 establishes reproducible local baselines over `transaction_features`. It does not create
risk scores, alerts, criminal labels, entity resolution, or a production model. The complete
machine-readable result is [`../benchmarks/results/phase-5.json`](../benchmarks/results/phase-5.json).

## Methodology

The experiment layer loads explicitly allowlisted numeric columns directly from Parquet through
Arrow into NumPy. It never infers predictors from arbitrary table columns and never combines
transaction, address, and IP entity rows. Three transaction feature families are supported:

- `transaction-only`: 14 value and structural measurements;
- `network-only`: 18 observation and temporal measurements, reduced to 16 in this generator after
  training-split constant detection;
- `all-eligible`: their union, reduced from 32 to 30 for the same reason.

Every model is an scikit-learn `Pipeline`. Median imputation, scaling where appropriate, and a
finite-matrix boundary check are fit or executed from the training partition only. Logistic
Regression, Isolation Forest, and LOF use scaling; Random Forest does not. Randomized operations
receive the root seed. Isolation Forest uses 200 trees and `contamination="auto"`; LOF is a
`novelty=True` comparison baseline. Logistic Regression is class-weighted and Random Forest uses
300 trees with bounded depth and leaf size. Exact settings and library versions live in every
`experiment.json`.

## Dataset semantics

The measured dataset contains 10,000 synthetic transaction entities and 17,984 network
observations generated with seed 42. Its transaction distribution is:

| Scenario class | Transactions |
| --- | ---: |
| baseline | 4,037 |
| high_fan_out_pattern | 1,501 |
| rapid_sequence_pattern | 1,473 |
| shared_network_pattern | 1,519 |
| high_value_pattern | 1,470 |

These are synthetic scenario classes describing generator mechanics. They are not criminal,
malicious, or investigative ground truth.

## Scenario truth limitation

The scenarios deliberately alter input/output counts, values, observation timing, and endpoint
reuse. Those changes map directly to Phase 4 measurements, making the full feature matrix highly
separable. Perfect supervised performance therefore verifies data flow, evaluation, and leakage
controls against known mechanics; it does not estimate performance on real Bitcoin traffic.

Truth is stored only in `scenario-truth.json` and joined inside the experiment layer. The feature
builder has no truth input. Predictions omit scenario labels and groups; a separate evaluation
Parquet holds them. The sidecar declares `not_criminal_ground_truth=true`.

## Leakage controls

- TXID, address, IP, timestamps, labels, groups, source IDs, and manifest IDs cannot become model
  predictors under the explicit feature policy.
- The reusable leakage audit rejects forbidden or unknown candidate columns.
- Preprocessing is inside the fitted pipeline and sees training rows only.
- Related addresses, hubs, chains, and IP pools are scoped to deterministic scenario groups.
- Group-aware splitting is the measured default; group IDs are transient split metadata, not
  predictors.
- Train/validation/test membership is persisted in `splits.parquet` and hashed into experiment
  identity.
- Temporal splitting preserves timestamp buckets and verifies ordered boundaries. A shared
  snapshot or single-cutoff feature build is still not a rolling point-in-time dataset.
- Constant columns are detected from training rows, excluded, and reported.

## Split strategy

The measured group split is 70/15/15: 7,000 training rows in 350 groups, 1,500 validation rows in
75 groups, and 1,500 test rows in 75 groups. There are zero overlapping groups. A random stratified
strategy exists only as a marked diagnostic baseline. The chronological strategy is useful for
ordering checks, but snapshot experiments use complete history and must not be presented as
historical prediction evidence.

## Anomaly baseline

Scenario labels are never passed to `fit`; they are used only after scoring. The project convention
is always higher `anomaly_score` means more anomalous.

| Model | Features | ROC-AUC | Average precision | Top-k capture |
| --- | ---: | ---: | ---: | ---: |
| Isolation Forest | 30 | 0.872 | 0.906 | 0.845 |
| Local Outlier Factor | 30 | 0.546 | 0.661 | 0.633 |

LOF is close to weak ranking performance here. It remains a comparison baseline, not a selected
production model.

## Supervised baselines

| Model | Features | Macro F1 | Weighted F1 | Balanced accuracy | Multiclass Brier |
| --- | ---: | ---: | ---: | ---: | ---: |
| Logistic Regression | 30 | 1.000 | 1.000 | 1.000 | 0.0000020 |
| Random Forest | 30 | 1.000 | 1.000 | 1.000 | 0.0000010 |

For both full-feature models, precision, recall, and F1 are 1.000 for baseline,
`high_fan_out_pattern`, `high_value_pattern`, `rapid_sequence_pattern`, and
`shared_network_pattern`. Probabilities are described only as model probabilities. The Brier score
is a diagnostic on synthetic held-out groups, not proof of real-world calibration.

## Per-scenario performance

Isolation Forest reveals the variation hidden by its overall ROC-AUC:

| Injected scenario vs baseline | ROC-AUC | Average precision | Top-k capture |
| --- | ---: | ---: | ---: |
| high_fan_out_pattern | 0.710 | 0.375 | 0.329 |
| high_value_pattern | 0.993 | 0.974 | 1.000 |
| rapid_sequence_pattern | 0.918 | 0.803 | 1.000 |
| shared_network_pattern | 0.849 | 0.638 | 1.000 |

The anomaly model detects high-value and timing/network scenarios far more readily than fan-out.
That is precisely why one aggregate score is insufficient.

## Feature ablation

| Logistic Regression feature family | Features | Macro F1 | Balanced accuracy | Brier |
| --- | ---: | ---: | ---: | ---: |
| transaction-only | 14 | 0.567 | 0.602 | 0.471 |
| network-only | 16 | 0.514 | 0.613 | 0.457 |
| all-eligible | 30 | 1.000 | 1.000 | 0.0000020 |

Transaction features perfectly separate the deliberately encoded fan-out and high-value classes
but confuse timing/network scenarios with baseline. Network features perfectly separate rapid and
shared-network patterns but cannot identify baseline or transaction-structure scenarios reliably.
The union closes those generator-specific gaps.

## Artifacts and reproducibility

An experiment directory contains a semantic manifest, metrics, feature-column policy, JSON and
Parquet split artifacts, predictions, machine-readable evaluation diagnostics, and
`model.joblib`. The experiment ID hashes the feature dataset, truth hash, split membership,
features, model configuration, seed, and library versions; `created_at` and output location are
excluded. Same-input reruns produced identical IDs, split membership, predictions, and metric
bytes.

Joblib is a pickle-compatible code-execution boundary. Model loading requires an explicit
trusted-local decision and verifies the exact manifest-declared SHA-256 before deserialization.
Never load downloaded, uploaded, or otherwise untrusted model files.

## Limitations

- Snapshot features use complete dataset history and are descriptive, not point-in-time-safe.
- Synthetic mechanics and class proportions do not represent operational Bitcoin traffic.
- No real labels, external validation set, rolling backtest, threshold selection, or probability
  calibration method was available.
- No address/IP models were forced because scenario truth maps cleanly only to transactions.
- Feature importance and coefficients are baseline diagnostics, not causal explanations.
- Phase 5 makes no claim about criminality, investigative risk, alert quality, or deployment model
  selection.
