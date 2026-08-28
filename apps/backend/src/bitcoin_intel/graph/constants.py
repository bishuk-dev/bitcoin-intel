from __future__ import annotations

GRAPH_SCHEMA_VERSION = "1.0.0"
SUPPORTED_GRAPH_SCHEMA_VERSIONS = frozenset({GRAPH_SCHEMA_VERSION})

NEO4J_VERSION = "2026.07.1"
GDS_VERSION = "2026.07.0"
APOC_VERSION = "2026.07.1"

GRAPH_DATABASE_NAME = "neo4j"

NODE_COUNTS = (
    "transactions",
    "addresses",
    "ip_addresses",
    "network_observations",
)

RELATIONSHIP_COUNTS = (
    "spent_in",
    "created_output",
    "observed_transaction",
    "source_ip",
    "destination_ip",
)

CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (
        "transaction_txid_unique",
        "CREATE CONSTRAINT transaction_txid_unique IF NOT EXISTS "
        "FOR (node:Transaction) REQUIRE node.txid IS UNIQUE",
    ),
    (
        "address_address_unique",
        "CREATE CONSTRAINT address_address_unique IF NOT EXISTS "
        "FOR (node:Address) REQUIRE node.address IS UNIQUE",
    ),
    (
        "ip_address_ip_unique",
        "CREATE CONSTRAINT ip_address_ip_unique IF NOT EXISTS "
        "FOR (node:IPAddress) REQUIRE node.ip IS UNIQUE",
    ),
    (
        "network_observation_id_unique",
        "CREATE CONSTRAINT network_observation_id_unique IF NOT EXISTS "
        "FOR (node:NetworkObservation) REQUIRE node.observation_id IS UNIQUE",
    ),
)

EXPECTED_CONSTRAINT_NAMES = frozenset(name for name, _ in CONSTRAINTS)
