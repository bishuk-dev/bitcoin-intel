from __future__ import annotations

import duckdb

TRANSACTION_SUMMARY_VIEW = "transaction_summary"

_TRANSACTION_SUMMARY_SQL = """
CREATE VIEW transaction_summary AS
WITH input_summary AS (
    SELECT
        txid,
        count(*) AS input_count,
        sum(amount_sats) AS total_input_sats
    FROM transaction_inputs
    GROUP BY txid
),
output_summary AS (
    SELECT
        txid,
        count(*) AS output_count,
        sum(amount_sats) AS total_output_sats
    FROM transaction_outputs
    GROUP BY txid
),
observation_summary AS (
    SELECT
        txid,
        count(*) AS network_observation_count,
        min(observed_at) AS first_observed_at,
        max(observed_at) AS last_observed_at
    FROM network_observations
    GROUP BY txid
)
SELECT
    transactions.txid,
    coalesce(input_summary.input_count, 0)::BIGINT AS input_count,
    coalesce(output_summary.output_count, 0)::BIGINT AS output_count,
    coalesce(input_summary.total_input_sats, 0::HUGEINT) AS total_input_sats,
    coalesce(output_summary.total_output_sats, 0::HUGEINT) AS total_output_sats,
    transactions.fee_sats,
    coalesce(observation_summary.network_observation_count, 0)::BIGINT
        AS network_observation_count,
    observation_summary.first_observed_at,
    observation_summary.last_observed_at
FROM transactions
LEFT JOIN input_summary USING (txid)
LEFT JOIN output_summary USING (txid)
LEFT JOIN observation_summary USING (txid)
"""


def register_analytical_views(connection: duckdb.DuckDBPyConnection) -> None:
    """Register derived views whose aggregates are safe from join multiplication."""

    connection.execute(_TRANSACTION_SUMMARY_SQL)
