# SIH 26146 — Project Context

## Project Identity

Problem Statement ID: 26146

Title: AI-Powered Monitoring & Analysis of Bitcoin Transaction Traffic

Organization: National Technical Research Organisation (NTRO)

Category: Software

Theme: Blockchain & Cybersecurity

---

# Objective

Build a complete offline Linux-based Bitcoin transaction intelligence and investigation platform.

The system ingests bulk blockchain-layer and network-layer metadata, correlates observations across those layers, constructs a heterogeneous investigative graph, applies genuine machine-learning models to identify anomalous or suspicious behaviour, clusters related entities, and produces ranked, explainable investigative leads.

The system is not a cryptocurrency wallet, blockchain explorer, or generic analytics dashboard.

It is an offline investigative intelligence platform.

---

# Primary Requirements

The platform must:

1. Ingest bulk data supplied as CSV, JSON, or XML.

2. Support fields including:

   * timestamp
   * source IP
   * destination IP
   * source port
   * destination port
   * transaction ID
   * input wallet addresses
   * output wallet addresses
   * input amounts
   * output amounts
   * transaction fee
   * script type
   * geographic country
   * ASN

3. Convert accepted source formats into a normalized canonical representation.

4. Persist normalized analytical datasets as Apache Parquet.

5. Correlate network-layer observations with blockchain-layer transaction information.

6. Construct a heterogeneous graph containing entities such as:

   * Transaction
   * Wallet
   * IP address
   * ASN
   * Country
   * inferred Entity/Cluster

7. Represent relationships including appropriate forms of:

   * spends-from
   * sends-to
   * observed-from
   * observed-to
   * co-spent
   * network-associated
   * temporally-correlated
   * clustered-with

8. Apply genuine ML models. Rule-only detection does not satisfy the project.

9. Support anomaly detection.

10. Support entity clustering or entity-resolution analysis.

11. Produce prioritized investigative alerts.

12. Every alert must contain evidence explaining why it was generated.

13. Every alert must expose an interpretable confidence/risk score.

14. Provide an interactive analyst dashboard.

15. Provide link-analysis / graph visualization.

16. Operate without internet access after deployment.

---

# Architecture Principles

The canonical analytical data format is Apache Parquet.

Parquet is the durable analytical source of truth.

DuckDB is used for local OLAP and SQL-based analytical workloads.

Neo4j is used for persistent property-graph storage, traversal and investigative graph queries.

Neo4j Graph Data Science provides production graph algorithms.

python-igraph may be used for high-performance local graph computations, experimentation, feature engineering and independent algorithm validation.

These systems have different responsibilities and should not be collapsed into one storage abstraction.

---

# Technology Stack

## Platform

Target:
Linux

Primary verification environment:
Ubuntu Linux

Development environment:
Windows with WSL2

Containerization:
Docker
Docker Compose

---

## Backend

Python 3.13

FastAPI

Pydantic

uv

pyproject.toml

uv.lock

---

## Data Engineering

Polars

PyArrow

Apache Arrow

Apache Parquet

DuckDB

CSV/JSON/XML parsers as ingestion adapters

---

## Graph

Neo4j Community Edition

Neo4j Graph Data Science Community Edition

APOC Core

Official Neo4j Python driver

python-igraph

Neo4j must be deployable entirely offline.

The final Neo4j image and required plugins must be pinned and packaged before deployment. Runtime internet downloads are forbidden.

---

## Machine Learning

scikit-learn

XGBoost and LightGBM as competing gradient-boosting candidates

PyTorch

PyTorch Geometric

HDBSCAN

SHAP

Exact production models must be selected through controlled experiments rather than chosen because they are fashionable.

---

# ML Philosophy

The detection system should use complementary signals.

Potential feature families include:

## Transaction features

* transaction value
* input/output counts
* fees
* fee ratios
* value distribution
* transaction frequency
* transaction size
* script characteristics

## Behavioural features

* transaction velocity
* burstiness
* inter-arrival times
* repeated counterparties
* fan-in behaviour
* fan-out behaviour
* consolidation
* fragmentation
* temporal patterns

## Graph features

* degree
* weighted degree
* PageRank
* betweenness where practical
* clustering coefficient
* triangle counts
* community membership
* k-core
* component statistics
* neighborhood risk
* flow-related statistics

## Network features

* observed IP diversity
* ASN diversity
* country diversity
* repeated network observations
* temporal IP/transaction correlations
* unusual ports or network relationships where justified by the dataset

---

# ML Evaluation

At minimum, compare strong baselines.

Examples:

Anomaly detection:

* Isolation Forest
* Local Outlier Factor
* other justified anomaly models

Supervised classification:

* Logistic Regression baseline
* Random Forest baseline
* XGBoost
* LightGBM
* other models only when justified

Graph learning:

* engineered graph features + conventional ML
  versus
* GraphSAGE
* GAT
* other GNN architecture only when justified

A Graph Neural Network must not be selected merely because the data forms a graph.

Use the simplest model that produces the strongest reliable result.

---

# Evaluation Metrics

Do not rely solely on accuracy.

For labelled suspicious-activity experiments consider:

* precision
* recall
* F1
* PR-AUC
* ROC-AUC where appropriate
* confusion matrix
* calibration
* false-positive rate

Class imbalance must be explicitly addressed.

Avoid all forms of train/test leakage.

Graph-based splitting and temporal splitting should be evaluated where applicable.

---

# Risk Engine

ML outputs are signals, not automatically final investigative conclusions.

The Risk Engine should combine evidence from categories such as:

* anomaly evidence
* classification evidence
* graph evidence
* behavioural evidence
* network correlation evidence
* entity association evidence

The final scoring method must be documented and testable.

Risk and confidence must not be arbitrary percentages.

The meaning of each score must be defined.

---

# Evidence Engine

Explainability is a first-class subsystem.

An alert should conceptually contain:

* entity identifier
* entity type
* risk score
* confidence
* contributing signals
* model evidence
* graph evidence
* behavioural evidence
* network evidence
* supporting transactions
* supporting relationships
* timestamps
* traceable provenance

Tree-model explanations may use SHAP.

Graph and deterministic findings should be explained directly.

Every explanation should answer:

"Why should an investigator inspect this entity?"

---

# Frontend

React 19

TypeScript

Vite 8

Node.js 24 LTS

TanStack Query

Zustand

Sigma.js

Graphology

Apache ECharts

MapLibre GL JS

---

# Frontend Philosophy

This should look and behave like an investigative workstation, not a generic admin panel.

Primary screens should eventually include concepts such as:

* investigative overview
* prioritized alert queue
* graph investigation
* transaction explorer
* wallet/entity profile
* network intelligence
* timeline
* geographical analysis
* model/evidence inspection

Large graphs must not be blindly sent to the browser.

The backend should provide bounded investigative subgraphs.

---

# Graph Visualization

Sigma.js + Graphology is the primary browser graph-rendering stack.

Graph queries should normally return focused subgraphs rather than entire databases.

Graph expansion should be progressive.

Examples:

* expand one hop
* expand selected relationship
* trace upstream
* trace downstream
* inspect cluster
* inspect associated IPs
* inspect suspicious neighborhood

---

# Data Architecture

Raw input must never be treated as trusted.

Pipeline:

SOURCE
→ ingestion
→ schema validation
→ canonical normalization
→ semantic validation
→ deduplication
→ correlation
→ Parquet persistence
→ analytical features
→ graph construction
→ ML inference
→ evidence
→ alerts

Original source information or provenance must remain traceable after transformation.

---

# Offline Requirement

The final application must operate without internet connectivity.

Forbidden runtime dependencies include:

* cloud APIs
* hosted databases
* cloud LLMs
* CDN-only frontend assets
* remote GeoIP services
* runtime package downloads
* runtime Docker image pulls
* remote map tile dependencies
* blockchain explorer APIs

Required resources must be packaged beforehand.

---

# Neo4j Offline Deployment

Use pinned Neo4j versions.

Package:

* Neo4j
* GDS
* APOC Core
* required configuration

into a reproducible Docker deployment.

Prepare final Docker images on an internet-connected build machine.

Export them using Docker image archives.

Offline deployment loads local images without contacting external registries.

Persist graph data using explicit Docker volumes/bind mounts.

---

# Testing

Backend:

pytest
pytest-asyncio where required
Hypothesis for property-based testing

Frontend:

Vitest
React Testing Library
Playwright

Test:

* parsers
* schemas
* transformations
* malformed input
* deduplication
* graph construction
* feature computation
* ML pipelines
* scoring
* evidence generation
* API contracts
* frontend critical workflows

---

# Engineering Quality

Python:

Ruff
mypy
strict type hints where practical

Frontend:

TypeScript strict mode
ESLint
Prettier

Important changes must pass:

tests
lint
type checking
build

before they are considered complete.

---

# Non-Goals

Do not turn the system into:

* a generic blockchain explorer
* a live Bitcoin node
* an online blockchain scraping system
* a cryptocurrency trading application
* an LLM wrapper
* a rule-only fraud detector
* an overengineered microservice platform

The goal is high-quality offline investigative intelligence.

---

# Core Engineering Rule

Every important project requirement must follow:

requirement
→ design
→ implementation
→ integration
→ verification

A technology or feature mentioned only in documentation does not count as implemented.
