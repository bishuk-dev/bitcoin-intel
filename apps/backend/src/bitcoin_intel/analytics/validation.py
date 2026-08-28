from __future__ import annotations

from dataclasses import dataclass

import duckdb

from bitcoin_intel.analytics.dataset import CANONICAL_TABLES, AnalyticalDataset
from bitcoin_intel.analytics.models import IntegrityIssue, IntegrityReport


@dataclass(frozen=True, slots=True)
class _IntegrityCheck:
    code: str
    message: str
    sql: str


_CHECKS = (
    _IntegrityCheck(
        "ORPHAN_TRANSACTION_INPUT",
        "transaction inputs reference missing transactions",
        """SELECT count(*) FROM transaction_inputs
        LEFT JOIN transactions USING (txid) WHERE transactions.txid IS NULL""",
    ),
    _IntegrityCheck(
        "ORPHAN_TRANSACTION_OUTPUT",
        "transaction outputs reference missing transactions",
        """SELECT count(*) FROM transaction_outputs
        LEFT JOIN transactions USING (txid) WHERE transactions.txid IS NULL""",
    ),
    _IntegrityCheck(
        "ORPHAN_NETWORK_OBSERVATION_TXID",
        "network observations reference missing transactions",
        """SELECT count(*) FROM network_observations
        LEFT JOIN transactions USING (txid) WHERE transactions.txid IS NULL""",
    ),
    _IntegrityCheck(
        "ORPHAN_NETWORK_OBSERVATION_SOURCE",
        "network observations reference missing source records",
        """SELECT count(*) FROM network_observations
        LEFT JOIN source_records USING (source_record_id)
        WHERE source_records.source_record_id IS NULL""",
    ),
    _IntegrityCheck(
        "ORPHAN_TRANSACTION_SOURCE_TXID",
        "transaction-source links reference missing transactions",
        """SELECT count(*) FROM transaction_sources
        LEFT JOIN transactions USING (txid) WHERE transactions.txid IS NULL""",
    ),
    _IntegrityCheck(
        "ORPHAN_TRANSACTION_SOURCE_RECORD",
        "transaction-source links reference missing source records",
        """SELECT count(*) FROM transaction_sources
        LEFT JOIN source_records USING (source_record_id)
        WHERE source_records.source_record_id IS NULL""",
    ),
    _IntegrityCheck(
        "ORPHAN_REJECTED_SOURCE_RECORD",
        "rejections reference missing source records",
        """SELECT count(*) FROM rejected_records
        LEFT JOIN source_records USING (source_record_id)
        WHERE source_records.source_record_id IS NULL""",
    ),
    _IntegrityCheck(
        "DUPLICATE_TRANSACTION",
        "transactions contain duplicate TXIDs",
        """SELECT coalesce(sum(item_count - 1), 0) FROM (
        SELECT count(*) AS item_count FROM transactions GROUP BY txid HAVING count(*) > 1)""",
    ),
    _IntegrityCheck(
        "DUPLICATE_TRANSACTION_INPUT",
        "transaction inputs contain duplicate (txid, input_index) keys",
        """SELECT coalesce(sum(item_count - 1), 0) FROM (
        SELECT count(*) AS item_count FROM transaction_inputs
        GROUP BY txid, input_index HAVING count(*) > 1)""",
    ),
    _IntegrityCheck(
        "DUPLICATE_TRANSACTION_OUTPUT",
        "transaction outputs contain duplicate (txid, output_index) keys",
        """SELECT coalesce(sum(item_count - 1), 0) FROM (
        SELECT count(*) AS item_count FROM transaction_outputs
        GROUP BY txid, output_index HAVING count(*) > 1)""",
    ),
    _IntegrityCheck(
        "DUPLICATE_NETWORK_OBSERVATION",
        "network observations contain duplicate observation IDs",
        """SELECT coalesce(sum(item_count - 1), 0) FROM (
        SELECT count(*) AS item_count FROM network_observations
        GROUP BY observation_id HAVING count(*) > 1)""",
    ),
    _IntegrityCheck(
        "DUPLICATE_TRANSACTION_SOURCE",
        "transaction-source links contain duplicate keys",
        """SELECT coalesce(sum(item_count - 1), 0) FROM (
        SELECT count(*) AS item_count FROM transaction_sources
        GROUP BY txid, source_record_id HAVING count(*) > 1)""",
    ),
    _IntegrityCheck(
        "DUPLICATE_SOURCE_RECORD",
        "source records contain duplicate source record IDs",
        """SELECT coalesce(sum(item_count - 1), 0) FROM (
        SELECT count(*) AS item_count FROM source_records
        GROUP BY source_record_id HAVING count(*) > 1)""",
    ),
    _IntegrityCheck(
        "NEGATIVE_INPUT_AMOUNT",
        "transaction inputs contain negative satoshi amounts",
        "SELECT count(*) FROM transaction_inputs WHERE amount_sats < 0",
    ),
    _IntegrityCheck(
        "NEGATIVE_OUTPUT_AMOUNT",
        "transaction outputs contain negative satoshi amounts",
        "SELECT count(*) FROM transaction_outputs WHERE amount_sats < 0",
    ),
    _IntegrityCheck(
        "NEGATIVE_TRANSACTION_FEE",
        "transactions contain negative satoshi fees",
        "SELECT count(*) FROM transactions WHERE fee_sats < 0",
    ),
    _IntegrityCheck(
        "NEGATIVE_INPUT_INDEX",
        "transaction inputs contain negative indexes",
        "SELECT count(*) FROM transaction_inputs WHERE input_index < 0",
    ),
    _IntegrityCheck(
        "NEGATIVE_OUTPUT_INDEX",
        "transaction outputs contain negative indexes",
        "SELECT count(*) FROM transaction_outputs WHERE output_index < 0",
    ),
)


def validate_analytical_dataset(dataset: AnalyticalDataset) -> IntegrityReport:
    with dataset.connect() as connection:
        return validate_connection(connection, dataset)


def validate_connection(
    connection: duckdb.DuckDBPyConnection, dataset: AnalyticalDataset
) -> IntegrityReport:
    issues: list[IntegrityIssue] = []
    for check in _CHECKS:
        row = connection.execute(check.sql).fetchone()
        if row is None:
            raise AssertionError(f"integrity check {check.code} returned no row")
        count = int(row[0])
        if count:
            issues.append(IntegrityIssue(check.code, count, check.message))

    for table_name in CANONICAL_TABLES:
        actual_row = connection.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()
        if actual_row is None:
            raise AssertionError(f"row count query for {table_name} returned no row")
        actual_count = int(actual_row[0])
        expected_count = dataset.manifest.tables[table_name].rows
        if actual_count != expected_count:
            issues.append(
                IntegrityIssue(
                    code="MANIFEST_ROW_COUNT_MISMATCH",
                    count=abs(actual_count - expected_count),
                    message=(
                        f"{table_name} contains {actual_count} rows but the manifest declares "
                        f"{expected_count}"
                    ),
                )
            )
    return IntegrityReport(tuple(issues))
