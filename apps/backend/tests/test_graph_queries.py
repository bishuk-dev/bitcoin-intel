from __future__ import annotations

from typing import cast

import pytest
from neo4j import Driver

from bitcoin_intel.graph.models import GraphNodeIdentity
from bitcoin_intel.graph.queries import GraphQueries


def test_graph_query_inputs_are_validated_before_database_access() -> None:
    queries = GraphQueries(cast(Driver, object()), "neo4j")

    with pytest.raises(ValueError, match="64 hexadecimal"):
        queries.transaction_neighborhood("not-a-txid")
    with pytest.raises(ValueError, match="does not appear to be"):
        queries.ip_observations("not-an-ip")
    with pytest.raises(ValueError, match="1 through 8"):
        queries.shortest_path(
            GraphNodeIdentity("address", "A"),
            GraphNodeIdentity("address", "B"),
            max_depth=9,
        )
    with pytest.raises(ValueError, match="unsupported graph node kind"):
        queries.shortest_path(
            GraphNodeIdentity("wallet", "A"),
            GraphNodeIdentity("address", "B"),
        )
