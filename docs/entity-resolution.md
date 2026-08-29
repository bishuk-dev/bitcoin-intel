# Conservative entity hypotheses

Phase 8 adds a rebuildable `entity-hypotheses` Parquet layer. It does **not** change canonical
transactions, the factual Neo4j graph, or Feature Schema v1/v2. An address remains an address;
candidate entities and communities are analytical hypotheses, never verified wallets, owners,
people, devices, guilt, or criminality.

## Intuition

Think of ownership inference as a courtroom evidence board with strict lanes:

- an unsuppressed multi-input co-spend may connect two addresses in the candidate-ownership lane;
- repeated network context can support an existing connection but cannot create one;
- behavioral similarity and graph community membership are useful neighborhoods, but neither is
  ownership evidence;
- every inferred connection retains its source and every transitive closure exposes its weak
  bridges.

This precision-first boundary is intentional. False merges are especially damaging because one
bad bridge can contaminate a large connected component.

## Technical explanation

The conservative engine sorts the distinct inputs of each transaction and encodes the multi-input
heuristic (MIH) as a compact lexicographic star. For inputs `A, B, C`, it records `A-B` and `A-C`;
this has the same connected-component result as all three pair combinations while storing `n-1`
edges instead of `n(n-1)/2`. A deterministic union-find consumes only strong, selected edges.

Before unioning, the collaborative detector records these factual signals:

- distinct input and output counts;
- repeated equal-output groups and maximum multiplicity;
- equal-output and balanced-output fractions.

The detector labels a transaction `collaborative_tx_suspected` only when all configured thresholds
pass. This is a heuristic suppression decision, not a definitive CoinJoin claim. No change-address,
peel-chain, script-wallet, UTXO-ownership, or network-ownership heuristic is present.

Candidate IDs are SHA-256 hashes over a canonical JSON array containing the method namespace and
sorted member addresses. They are stable under source ordering and do not use database IDs. The
membership table marks whether an address has direct accepted evidence to the lexicographic
candidate anchor or appears only through transitive closure. `membership_support` is the fraction
of the candidate's distinct merge transactions incident to that address; it is not a probability.

Bridge diagnostics run on the accepted simple evidence graph. An edge is fragile when it is an
igraph bridge backed by exactly one transaction. `robustness_score` is
`1 - fragile_bridge_count / strong_evidence_edge_count` (or `1` for a singleton); it is a structural
diagnostic, not confidence or risk.

Network support is encoded independently as a lexicographic star over addresses associated with
each source IP. The evidence source stores a content hash rather than repeating the endpoint, and
`merge_selected` is always false. This bounded `n-1` representation preserves cross-candidate
shared-infrastructure context without an all-pairs explosion and makes it impossible for an IP,
ASN, or country alone to merge addresses.

## Separate community outputs

Behavioral communities use the explicit address-feature allowlist in the manifest. Median
imputation and standard scaling precede sklearn HDBSCAN. Addresses are sorted before fitting;
identities, timestamps, truth, candidate IDs, and labels are excluded. HDBSCAN label `-1` is
preserved as `is_noise=true`, a null community ID, and size zero.

Topological communities use deterministic, seeded igraph Leiden modularity over an undirected,
weighted all-pairs address co-occurrence projection per transaction. Their content-addressed IDs
live in `topological_communities`, separate from `behavioral_communities` and
`candidate_memberships`. Co-transaction proximity is not ownership.

## Artifact contract

Every table is explicit-schema, Zstandard-compressed Parquet at `<table>/part-00000.parquet`:

| Table | Meaning |
| --- | --- |
| `candidate_entities` | component size, evidence, density, bridge, and robustness summaries |
| `candidate_memberships` | one conservative candidate assignment per canonical address |
| `ownership_evidence` | auditable strong/suppressed/supporting evidence rows |
| `collaborative_transactions` | detector inputs and suppression decision for every transaction |
| `behavioral_communities` | HDBSCAN membership with explicit noise |
| `topological_communities` | separate Leiden membership |

`entity-manifest.json` binds canonical and feature manifest hashes, schema/method versions,
detector thresholds, preprocessing, projection semantics, and table hashes. Build time is excluded
from the semantic dataset ID. Publication uses a same-filesystem staging directory, validates the
complete staged store, refuses overwrites, then atomically renames it.

## CLI

```bash
uv run bitcoin-intel entity build \
  --dataset ./dataset --features ./features --output ./entity-hypotheses \
  --collaborative-min-inputs 3 --collaborative-min-outputs 3 \
  --collaborative-min-equal-outputs 2 --collaborative-min-equal-fraction 0.5

uv run bitcoin-intel entity validate \
  --entities ./entity-hypotheses --dataset ./dataset --features ./features

uv run bitcoin-intel entity evaluate \
  --entities ./entity-hypotheses --dataset ./dataset --features ./features \
  --truth ./entity-truth.json --partition test
```

Evaluation truth is accepted only by `entity evaluate`. It never enters canonical ingestion,
feature construction, or entity building. The evaluator reports pairwise precision/recall/F1,
B-cubed, ARI, AMI, over-merge and fragmentation rates, per-entity coverage, and collaborative
false-merge rate for raw MIH, suppression, and the final conservative output.

## Real-world use and pitfalls

Investigative platforms use conservative clusters to reduce repeated review across plausibly
co-controlled addresses and use communities to find neighborhoods worth inspecting. Analysts must
still examine transaction structure, bridge evidence, time, and provenance.

Important failure modes remain:

- collaborative spends can defeat raw MIH and incomplete detectors can still miss them;
- services, custodians, and shared infrastructure can create large observational neighborhoods;
- transitive closure amplifies a single false edge, which is why bridges remain visible;
- HDBSCAN and Leiden reveal similarity or topology, not ownership;
- synthetic benchmark truth measures this generator, not real-world ground truth.

The engine deliberately does not add aggressive change or peel heuristics without an independently
validated evidence contract.
