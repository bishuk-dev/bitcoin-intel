from __future__ import annotations

from typing import Any

from neo4j import Driver

from bitcoin_intel.analytics import AnalyticalDataset
from bitcoin_intel.graph.models import (
    GraphCounts,
    GraphIntegrityIssue,
    GraphIntegrityReport,
)

_SAMPLE_SIZE = 100


def validate_graph(
    driver: Driver, database: str, canonical_dataset: AnalyticalDataset
) -> GraphIntegrityReport:
    canonical_counts = _canonical_counts(canonical_dataset)
    graph_counts = _graph_counts(driver, database)
    issues: list[GraphIntegrityIssue] = []
    for field in GraphCounts.__dataclass_fields__:
        actual = getattr(graph_counts, field)
        expected = getattr(canonical_counts, field)
        if actual != expected:
            issues.append(
                GraphIntegrityIssue(
                    "GRAPH_COUNT_MISMATCH",
                    abs(actual - expected),
                    f"{field} graph count is {actual}; canonical count is {expected}",
                )
            )
    _validate_graph_cardinality(driver, database, issues)
    _validate_sample_values(driver, database, canonical_dataset, issues)
    _validate_provenance(driver, database, canonical_dataset, issues)
    return GraphIntegrityReport(graph_counts, canonical_counts, tuple(issues))


def _canonical_counts(dataset: AnalyticalDataset) -> GraphCounts:
    with dataset.connect() as connection:
        row = connection.execute(
            """SELECT
                (SELECT count(*) FROM transactions),
                (SELECT count(*) FROM (
                    SELECT address FROM transaction_inputs
                    UNION SELECT address FROM transaction_outputs
                )),
                (SELECT count(*) FROM (
                    SELECT src_ip AS ip FROM network_observations
                    UNION SELECT dst_ip AS ip FROM network_observations
                )),
                (SELECT count(*) FROM network_observations),
                (SELECT count(*) FROM transaction_inputs),
                (SELECT count(*) FROM transaction_outputs)
            """
        ).fetchone()
    if row is None:
        raise AssertionError("canonical graph count query returned no row")
    observations = int(row[3])
    return GraphCounts(
        transactions=int(row[0]),
        addresses=int(row[1]),
        ip_addresses=int(row[2]),
        network_observations=observations,
        spent_in=int(row[4]),
        created_output=int(row[5]),
        observed_transaction=observations,
        source_ip=observations,
        destination_ip=observations,
    )


def _graph_counts(driver: Driver, database: str) -> GraphCounts:
    with driver.session(database=database) as session:
        record = session.run(
            """CALL () {
                MATCH (node:Transaction) RETURN count(node) AS transactions
            } CALL () {
                MATCH (node:Address) RETURN count(node) AS addresses
            } CALL () {
                MATCH (node:IPAddress) RETURN count(node) AS ip_addresses
            } CALL () {
                MATCH (node:NetworkObservation) RETURN count(node) AS network_observations
            } CALL () {
                MATCH ()-[relationship:SPENT_IN]->() RETURN count(relationship) AS spent_in
            } CALL () {
                MATCH ()-[relationship:CREATED_OUTPUT]->()
                RETURN count(relationship) AS created_output
            } CALL () {
                MATCH ()-[relationship:OBSERVED_TRANSACTION]->()
                RETURN count(relationship) AS observed_transaction
            } CALL () {
                MATCH ()-[relationship:SOURCE_IP]->() RETURN count(relationship) AS source_ip
            } CALL () {
                MATCH ()-[relationship:DESTINATION_IP]->()
                RETURN count(relationship) AS destination_ip
            }
            RETURN transactions, addresses, ip_addresses, network_observations, spent_in,
            created_output, observed_transaction, source_ip, destination_ip"""
        ).single(strict=True)
    return GraphCounts(**{field: int(record[field]) for field in GraphCounts.__dataclass_fields__})


def _validate_graph_cardinality(
    driver: Driver, database: str, issues: list[GraphIntegrityIssue]
) -> None:
    checks = (
        (
            "INVALID_SPENT_IN_ENDPOINT",
            """MATCH (start)-[relationship:SPENT_IN]->(end)
            WHERE NOT 'Address' IN labels(start) OR NOT 'Transaction' IN labels(end)
            RETURN count(relationship) AS count""",
            "SPENT_IN endpoints must be Address -> Transaction",
        ),
        (
            "INVALID_CREATED_OUTPUT_ENDPOINT",
            """MATCH (start)-[relationship:CREATED_OUTPUT]->(end)
            WHERE NOT 'Transaction' IN labels(start) OR NOT 'Address' IN labels(end)
            RETURN count(relationship) AS count""",
            "CREATED_OUTPUT endpoints must be Transaction -> Address",
        ),
        (
            "INVALID_OBSERVED_TRANSACTION_ENDPOINT",
            """MATCH (start)-[relationship:OBSERVED_TRANSACTION]->(end)
            WHERE NOT 'NetworkObservation' IN labels(start)
               OR NOT 'Transaction' IN labels(end)
            RETURN count(relationship) AS count""",
            "OBSERVED_TRANSACTION endpoints must be NetworkObservation -> Transaction",
        ),
        (
            "INVALID_SOURCE_IP_ENDPOINT",
            """MATCH (start)-[relationship:SOURCE_IP]->(end)
            WHERE NOT 'NetworkObservation' IN labels(start) OR NOT 'IPAddress' IN labels(end)
            RETURN count(relationship) AS count""",
            "SOURCE_IP endpoints must be NetworkObservation -> IPAddress",
        ),
        (
            "INVALID_DESTINATION_IP_ENDPOINT",
            """MATCH (start)-[relationship:DESTINATION_IP]->(end)
            WHERE NOT 'NetworkObservation' IN labels(start) OR NOT 'IPAddress' IN labels(end)
            RETURN count(relationship) AS count""",
            "DESTINATION_IP endpoints must be NetworkObservation -> IPAddress",
        ),
        (
            "INVALID_OBSERVATION_CARDINALITY",
            """MATCH (observation:NetworkObservation)
            WHERE COUNT { (observation)-[:OBSERVED_TRANSACTION]->(:Transaction) } <> 1
               OR COUNT { (observation)-[:SOURCE_IP]->(:IPAddress) } <> 1
               OR COUNT { (observation)-[:DESTINATION_IP]->(:IPAddress) } <> 1
            RETURN count(observation) AS count""",
            "each observation must have exactly one transaction, source IP, and destination IP",
        ),
    )
    with driver.session(database=database) as session:
        for code, query, message in checks:
            count = int(session.run(query).single(strict=True)["count"])
            if count:
                issues.append(GraphIntegrityIssue(code, count, message))


def _validate_sample_values(
    driver: Driver,
    database: str,
    dataset: AnalyticalDataset,
    issues: list[GraphIntegrityIssue],
) -> None:
    with dataset.connect() as connection:
        samples = (
            (
                "TRANSACTION_PROPERTY_MISMATCH",
                """SELECT txid, fee_sats, script_type FROM transactions
                ORDER BY txid LIMIT ?""",
                """UNWIND $rows AS expected
                OPTIONAL MATCH (node:Transaction {txid: expected.txid})
                WITH expected, node WHERE node IS NULL OR node.fee_sats <> expected.fee_sats
                    OR (node.script_type IS NULL) <> (expected.script_type IS NULL)
                    OR (node.script_type IS NOT NULL AND expected.script_type IS NOT NULL
                        AND node.script_type <> expected.script_type)
                RETURN count(*) AS count""",
                ("txid", "fee_sats", "script_type"),
            ),
            (
                "SPENT_IN_PROPERTY_MISMATCH",
                """SELECT address, txid, input_index, amount_sats FROM transaction_inputs
                ORDER BY txid, input_index LIMIT ?""",
                """UNWIND $rows AS expected
                OPTIONAL MATCH (:Address {address: expected.address})-[use:SPENT_IN]->
                    (:Transaction {txid: expected.txid})
                WHERE use.input_index = expected.input_index
                WITH expected, use WHERE use IS NULL OR use.amount_sats <> expected.amount_sats
                RETURN count(*) AS count""",
                ("address", "txid", "input_index", "amount_sats"),
            ),
            (
                "CREATED_OUTPUT_PROPERTY_MISMATCH",
                """SELECT txid, address, output_index, amount_sats FROM transaction_outputs
                ORDER BY txid, output_index LIMIT ?""",
                """UNWIND $rows AS expected
                OPTIONAL MATCH (:Transaction {txid: expected.txid})-[use:CREATED_OUTPUT]->
                    (:Address {address: expected.address})
                WHERE use.output_index = expected.output_index
                WITH expected, use WHERE use IS NULL OR use.amount_sats <> expected.amount_sats
                RETURN count(*) AS count""",
                ("txid", "address", "output_index", "amount_sats"),
            ),
            (
                "OBSERVATION_PROPERTY_MISMATCH",
                """SELECT observation_id, observed_at, src_port, dst_port,
                reported_geo_country, reported_asn, source_record_id
                FROM network_observations ORDER BY observation_id LIMIT ?""",
                """UNWIND $rows AS expected
                OPTIONAL MATCH (node:NetworkObservation {observation_id: expected.observation_id})
                WITH expected, node WHERE node IS NULL
                    OR node.observed_at.epochSeconds <> expected.observed_at.epochSeconds
                    OR node.observed_at.nanosecond <> expected.observed_at.nanosecond
                    OR node.src_port <> expected.src_port OR node.dst_port <> expected.dst_port
                    OR (node.reported_geo_country IS NULL) <>
                        (expected.reported_geo_country IS NULL)
                    OR (node.reported_geo_country IS NOT NULL
                        AND expected.reported_geo_country IS NOT NULL
                        AND node.reported_geo_country <> expected.reported_geo_country)
                    OR (node.reported_asn IS NULL) <> (expected.reported_asn IS NULL)
                    OR (node.reported_asn IS NOT NULL AND expected.reported_asn IS NOT NULL
                        AND node.reported_asn <> expected.reported_asn)
                    OR node.source_record_id <> expected.source_record_id
                RETURN count(*) AS count""",
                (
                    "observation_id",
                    "observed_at",
                    "src_port",
                    "dst_port",
                    "reported_geo_country",
                    "reported_asn",
                    "source_record_id",
                ),
            ),
        )
        prepared: list[tuple[str, str, list[dict[str, Any]]]] = []
        for code, canonical_query, graph_query, columns in samples:
            canonical_rows = connection.execute(canonical_query, [_SAMPLE_SIZE]).fetchall()
            prepared.append(
                (
                    code,
                    graph_query,
                    [dict(zip(columns, row, strict=True)) for row in canonical_rows],
                )
            )
    with driver.session(database=database) as session:
        for code, query, expected_rows in prepared:
            count = int(session.run(query, rows=expected_rows).single(strict=True)["count"])
            if count:
                issues.append(
                    GraphIntegrityIssue(
                        code,
                        count,
                        f"up to {_SAMPLE_SIZE} deterministic canonical rows were compared",
                    )
                )


def _validate_provenance(
    driver: Driver,
    database: str,
    dataset: AnalyticalDataset,
    issues: list[GraphIntegrityIssue],
) -> None:
    with dataset.connect() as connection:
        rows = connection.execute("SELECT source_record_id FROM source_records").fetchall()
    canonical_ids = {str(row[0]) for row in rows}
    missing = 0
    with driver.session(database=database) as session:
        result = session.run(
            "MATCH (node:NetworkObservation) RETURN node.source_record_id AS source_record_id"
        )
        for record in result:
            if str(record["source_record_id"]) not in canonical_ids:
                missing += 1
    if missing:
        issues.append(
            GraphIntegrityIssue(
                "ORPHAN_OBSERVATION_PROVENANCE",
                missing,
                "NetworkObservation.source_record_id is absent from canonical source_records",
            )
        )
