from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.graph.config import GraphSettings
from bitcoin_intel.graph.connection import GraphConnection
from bitcoin_intel.graph.constants import (
    APOC_VERSION,
    EXPECTED_CONSTRAINT_NAMES,
    GDS_VERSION,
    NEO4J_VERSION,
)
from bitcoin_intel.graph.gds import verify_gds_foundation
from bitcoin_intel.graph.models import GraphNodeIdentity
from bitcoin_intel.graph.queries import GraphQueries
from bitcoin_intel.graph.schema import graph_constraint_names, plugin_versions
from bitcoin_intel.graph.validation import validate_graph

pytestmark = pytest.mark.integration


def test_dockerized_graph_runtime_and_foundational_queries() -> None:
    if os.environ.get("NEO4J_INTEGRATION") != "1":
        pytest.skip("set NEO4J_INTEGRATION=1 after importing the integration dataset")
    raw_dataset = os.environ.get("GRAPH_TEST_DATASET")
    if raw_dataset is None:
        pytest.fail("GRAPH_TEST_DATASET is required when NEO4J_INTEGRATION=1")
    dataset = AnalyticalDataset(Path(raw_dataset))
    settings = GraphSettings()  # type: ignore[call-arg]

    with GraphConnection(settings).connect() as driver:
        versions = plugin_versions(driver, settings.neo4j_database)
        assert versions.neo4j == NEO4J_VERSION
        assert versions.edition.lower() == "community"
        assert versions.gds == GDS_VERSION
        assert versions.apoc == APOC_VERSION
        assert set(graph_constraint_names(driver, settings.neo4j_database)) == set(
            EXPECTED_CONSTRAINT_NAMES
        )

        integrity = validate_graph(driver, settings.neo4j_database, dataset)
        assert integrity.is_valid, integrity.issues

        queries = GraphQueries(driver, settings.neo4j_database)
        transaction = queries.transaction_neighborhood("a" * 64)
        assert transaction is not None
        assert len(transaction.inputs) == 2
        assert len(transaction.outputs) == 2
        assert len(transaction.observations) == 2
        assert transaction.observations[0].observed_at.utcoffset() == timedelta(0)
        assert {item.role for item in queries.address_transactions("BothAddress").uses} == {
            "input",
            "output",
        }
        assert {item.role for item in queries.ip_observations("192.0.2.1").uses} == {
            "source",
            "destination",
        }
        path = queries.shortest_path(
            GraphNodeIdentity("address", "InputOnly"),
            GraphNodeIdentity("address", "OutputOnly"),
            max_depth=2,
        )
        assert path is not None
        assert path.relationship_types == ("SPENT_IN", "CREATED_OUTPUT")

        with driver.session(database=settings.neo4j_database) as session:
            types = session.run(
                """MATCH (transaction:Transaction {txid: $txid})
                MATCH (observation:NetworkObservation)-[:OBSERVED_TRANSACTION]->(transaction)
                MATCH (:Address)-[input:SPENT_IN]->(transaction)
                RETURN valueType(transaction.fee_sats) AS fee_type,
                    valueType(observation.observed_at) AS timestamp_type,
                    valueType(observation.src_port) AS port_type,
                    valueType(input.amount_sats) AS amount_type LIMIT 1""",
                txid="a" * 64,
            ).single(strict=True)
            nulls = session.run(
                """MATCH (transaction:Transaction {txid: $null_txid})
                MATCH (observation:NetworkObservation)-[:OBSERVED_TRANSACTION]->
                    (:Transaction {txid: $observed_txid})
                WHERE observation.reported_geo_country IS NULL
                    AND observation.reported_asn IS NULL
                RETURN transaction.script_type IS NULL AS script_is_null,
                    observation.reported_geo_country IS NULL AS country_is_null,
                    observation.reported_asn IS NULL AS asn_is_null""",
                null_txid="c" * 64,
                observed_txid="a" * 64,
            ).single(strict=True)
        assert dict(types) == {
            "fee_type": "INTEGER NOT NULL",
            "timestamp_type": "ZONED DATETIME NOT NULL",
            "port_type": "INTEGER NOT NULL",
            "amount_type": "INTEGER NOT NULL",
        }
        assert all(bool(value) for value in nulls.values())

        gds = verify_gds_foundation(driver, settings.neo4j_database)
        assert (
            gds.node_count == integrity.graph_counts.transactions + integrity.graph_counts.addresses
        )
        assert gds.relationship_count == (
            integrity.graph_counts.spent_in + integrity.graph_counts.created_output
        )
        assert gds.component_count > 0
