from __future__ import annotations

import duckdb
import igraph as ig
import pyarrow as pa


def build_component_size_table(connection: duckdb.DuckDBPyConnection) -> pa.Table:
    """Compute WCC sizes for the scoped factual Address--Transaction projection.

    DuckDB assigns deterministic disjoint integer namespaces for addresses and transactions before
    the edge list crosses into Python. The in-memory projection is discarded after this function;
    only component size is returned because igraph membership IDs are not stable semantic IDs.
    """

    try:
        connection.execute(
            """CREATE TEMP TABLE feature_graph_addresses AS
            SELECT address, (row_number() OVER (ORDER BY address) - 1)::BIGINT AS vertex_id
            FROM (SELECT address FROM address_transactions GROUP BY address)"""
        )
        address_count = _scalar(connection, "SELECT count(*) FROM feature_graph_addresses")
        connection.execute(
            """CREATE TEMP TABLE feature_graph_transactions AS
            SELECT txid, (? + row_number() OVER (ORDER BY txid) - 1)::BIGINT AS vertex_id
            FROM scoped_transactions""",
            [address_count],
        )
        transaction_count = _scalar(connection, "SELECT count(*) FROM feature_graph_transactions")
        address_rows = connection.execute(
            "SELECT address, vertex_id FROM feature_graph_addresses ORDER BY vertex_id"
        ).fetchall()
        if not address_rows:
            return pa.table(
                {
                    "address": pa.array([], type=pa.string()),
                    "bipartite_component_size": pa.array([], type=pa.int64()),
                }
            )

        edge_cursor = connection.execute(
            """SELECT a.vertex_id, t.vertex_id
            FROM address_transactions e
            JOIN feature_graph_addresses a USING (address)
            JOIN feature_graph_transactions t USING (txid)
            ORDER BY a.vertex_id, t.vertex_id"""
        )
        edges: list[tuple[int, int]] = []
        while rows := edge_cursor.fetchmany(65_536):
            edges.extend((int(source), int(target)) for source, target in rows)
        graph = ig.Graph(n=address_count + transaction_count, edges=edges, directed=False)
        components = graph.connected_components(mode="weak")
        component_sizes = components.sizes()
        membership = components.membership
        return pa.table(
            {
                "address": pa.array((str(row[0]) for row in address_rows), type=pa.string()),
                "bipartite_component_size": pa.array(
                    (int(component_sizes[membership[int(row[1])]]) for row in address_rows),
                    type=pa.int64(),
                ),
            }
        )
    finally:
        connection.execute("DROP TABLE IF EXISTS feature_graph_transactions")
        connection.execute("DROP TABLE IF EXISTS feature_graph_addresses")


def _scalar(connection: duckdb.DuckDBPyConnection, query: str) -> int:
    row = connection.execute(query).fetchone()
    if row is None:
        raise AssertionError("graph projection count returned no row")
    return int(row[0])
