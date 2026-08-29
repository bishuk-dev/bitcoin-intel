# Phase 7 Classical ML Model Selection

Phase 7 selects a small classical-ML set for later intelligence-engine work. It does not create
risk scores, alerts, criminal labels, entity resolution, or production explanations. The complete
machine-readable evidence is in
[`../benchmarks/results/phase-7.json`](../benchmarks/results/phase-7.json), and the registry pointer
is [`../benchmarks/results/model-selection.json`](../benchmarks/results/model-selection.json).

## Intuition

Phase 5's perfect supervised scores proved the experiment plumbing worked, but its generator used
fixed, obvious signatures. `challenge-v1` instead makes baseline activity sometimes look unusual
and makes injected behaviour range from weak to strong. The resulting distributions overlap, so a
model must combine imperfect clues instead of learning one exact threshold.

## Evaluation contract

- The 20,000-transaction primary dataset contains 1,250 identity groups and 79,554 observations.
- Group-aware 70/15/15 splitting has zero group overlap. IDs, group IDs, timestamps, truth,
  intensity, and secondary tags are forbidden model inputs.
- Stage 1 screened one explicit configuration for every contender using train/validation only.
- Stage 2 evaluated exactly ten deterministic configurations for each of the two competitive
  models, Logistic Regression and Random Forest. It read zero test rows.
- The held-out test was evaluated once per selected candidate configuration. Five deterministic
  group splits were used for finalist stability; selection uses their validation results.
- Feature v2 is primary. Controlled MMDB countries and ASNs overlap every scenario; behavioural
  diversity and endpoint-match rates can help, but a country or ASN identity cannot be a class ID.
- A shared snapshot is not a rolling point-in-time feature store. Temporal evaluation was omitted
  rather than mislabeled as point-in-time prediction.

## Supervised comparison

Macro F1 under the group-safe test split is primary for reporting. The selection itself was fixed
from validation results before these test values were inspected.

| Model | Features | Train Macro F1 | Validation Macro F1 | Test Macro F1 | Balanced accuracy | Log loss | Brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic Regression | 36 | 0.4894 | 0.4831 | 0.4875 | 0.5300 | 1.1859 | 0.6362 |
| Random Forest | 36 | 0.7468 | 0.4956 | 0.4821 | 0.5150 | 1.1356 | 0.6215 |
| HistGradientBoosting | 36 | 0.6222 | 0.4770 | 0.4666 | 0.4831 | 1.0628 | 0.5818 |
| XGBoost | 36 | 0.7395 | 0.4755 | 0.4687 | 0.4811 | 1.0641 | 0.5837 |
| LightGBM | 36 | 0.8299 | 0.4740 | 0.4611 | 0.4731 | 1.0895 | 0.5952 |

The boosting implementations have better probability diagnostics than Logistic Regression but do
not have better Macro F1 or scenario balance here. This benchmark does not support carrying three
boosting engines forward solely because they are more sophisticated. Their larger train-to-
validation gaps, especially LightGBM's 0.8299 to 0.4740, are additional evidence against selecting
on training fit.

## Preferred supervised model

**Logistic Regression is preferred.** Random Forest's validation Macro F1 is 0.0125 higher, so both
entered the predeclared 0.03 performance gate. Logistic Regression then ranked higher across the
complete decision matrix: it is slightly more stable, trains 2.7 times faster, infers roughly 15
times faster, produces a 3.6 KiB model instead of 17.6 MiB, has standardized coefficients, and is
the simpler offline dependency. Its test Macro F1 is also 0.0054 higher, although that test result
was not used for selection.

Random Forest remains the one fallback because it has the best validation Macro F1 and different
nonlinear inductive bias. No calibrated variant is retained. The preferred experiment ID is
`81e4d126...cb7f4b`; the fallback is `92e487fb...e221a0`.

The weighted decision matrix first excludes models more than 0.03 Macro F1 below the best finalist,
then considers scenario balance, weak-pattern recall, Brier score, seed stability, runtime,
artifact size, diagnostic suitability, and offline deployment. It is regression-tested so a much
weaker tiny model cannot win on efficiency alone.

## Per-scenario and intensity behaviour

Preferred-model test metrics:

| Scenario | Precision | Recall | F1 |
| --- | ---: | ---: | ---: |
| baseline | 0.6491 | 0.4317 | 0.5185 |
| high_fan_out_pattern | 0.4039 | 0.5561 | 0.4679 |
| high_value_pattern | 0.3039 | 0.2450 | 0.2713 |
| rapid_sequence_pattern | 0.5904 | 0.9349 | 0.7238 |
| shared_network_pattern | 0.4325 | 0.4823 | 0.4561 |

| Scenario | Weak recall | Medium recall | Strong recall |
| --- | ---: | ---: | ---: |
| high_fan_out_pattern | 0.2426 | 0.6939 | 0.8765 |
| high_value_pattern | 0.1854 | 0.2414 | 0.3608 |
| rapid_sequence_pattern | 0.9086 | 0.9503 | 0.9639 |
| shared_network_pattern | 0.3312 | 0.5027 | 0.6762 |

High-value behaviour is hardest because the baseline deliberately has a wide heavy-tailed value
distribution. Weak fan-out and shared-network patterns are also substantially harder than their
strong variants. This is intended overlap, not arbitrary label noise.

## Feature ablation and Feature v2

| Preferred-model family | Features | Macro F1 | Rapid recall | Shared-network recall |
| --- | ---: | ---: | ---: | ---: |
| transaction only | 14 | 0.2805 | 0.0521 | 0.0332 |
| transaction + temporal | 26 | 0.4454 | 0.9501 | 0.3407 |
| transaction + network | 20 | 0.3994 | 0.5271 | 0.3252 |
| transaction + network + enrichment | 24 | 0.3926 | 0.5054 | 0.4912 |
| all eligible | 36 | 0.4875 | 0.9349 | 0.4823 |

The important NTRO answer is **yes, network and temporal information materially improve the
network-oriented scenarios over blockchain-only features**. Rapid recall rises from 0.0521 to
0.9349 and shared-network recall from 0.0332 to 0.4823 with all eligible inputs.

The controlled v1/v2 comparison is more nuanced. Overall Macro F1 is effectively unchanged and
slightly lower in v2 (0.4891 to 0.4875), while shared-network recall improves from 0.3142 to 0.4823.
Rapid recall moves from 0.9436 to 0.9349. GeoIP/ASN-derived endpoint match behaviour therefore adds
scenario-specific value, not a general accuracy improvement. Enriched facts remain useful to
investigators even where they do not improve the aggregate model metric.

## Calibration

Sigmoid calibration was fit through three training-only folds. It improves both Brier and log loss
for both finalists, but materially lowers Macro F1:

| Model | Variant | Validation Brier | Test Brier | Test log loss | Test Macro F1 |
| --- | --- | ---: | ---: | ---: | ---: |
| Logistic Regression | raw | 0.6336 | 0.6362 | 1.1859 | 0.4875 |
| Logistic Regression | sigmoid | 0.6162 | 0.6186 | 1.1617 | 0.3110 |
| Random Forest | raw | 0.6167 | 0.6215 | 1.1356 | 0.4821 |
| Random Forest | sigmoid | 0.5954 | 0.5986 | 1.1270 | 0.4336 |

The calibrated variants are not selected. Raw probabilities remain model probabilities, not
investigative confidence or risk.

## Anomaly model selection

| Model | ROC-AUC | PR-AUC | Top 1% capture | Top 5% capture | Top 10% capture |
| --- | ---: | ---: | ---: | ---: | ---: |
| Isolation Forest | 0.5861 | 0.6758 | 0.0133 | 0.0647 | 0.1267 |
| LOF | 0.5368 | 0.6293 | 0.0100 | 0.0564 | 0.1101 |
| PCA reconstruction | 0.5529 | 0.6516 | 0.0133 | 0.0614 | 0.1206 |

Isolation Forest remains preferred because it leads both ranking metrics and top-5/top-10 capture.
PCA is retained only as a lightweight diagnostic comparator. Operational capture is weak: no
anomaly score is treated as risk, and labels never enter anomaly fitting.

## Stability and limitations

Across five group seeds, Logistic Regression validation Macro F1 is `0.4811 ± 0.0059`; Random
Forest is `0.4925 ± 0.0082`. Shared-network F1 is the most variable class for both. No advanced
model reaches 0.99, so the perfect-score escalation is not triggered. The mandatory general audit
still passes: zero forbidden truth fields, identifier names, cross-group identities, or
single-class port/script/IP-prefix values; zero split overlap; and the best single-feature stump
(`max_observations_5m`) reaches only 0.2659 Macro F1.

These results measure recovery of synthetic mechanics under controlled distributions. They do not
estimate criminality, investigator utility, real Bitcoin prevalence, domain shift, or live model
quality. A real labeled/curated evaluation regime and point-in-time feature snapshots are required
before production use.
