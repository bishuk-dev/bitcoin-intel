from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from typing import Any, Literal, cast

import duckdb

from bitcoin_intel.analytics.models import (
    AddressActivitySummary,
    AsnActivitySummary,
    HighFeeTransaction,
    IpActivitySummary,
    NetworkObservation,
    TemporalActivity,
    Transaction,
    TransactionDetail,
    TransactionInput,
    TransactionOutput,
    TransactionProvenance,
    TransactionSummary,
)

TimeBucket = Literal["hour", "day"]
_TIME_BUCKET_SQL: dict[TimeBucket, str] = {
    "hour": "date_trunc('hour', observed_at)",
    "day": "date_trunc('day', observed_at)",
}


class AnalyticalQueries:
    """Typed, parameterized analytical operations over a registered dataset session."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def transaction(self, txid: str) -> TransactionDetail | None:
        normalized_txid = _normalize_txid(txid)
        transaction_row = self._connection.execute(
            "SELECT txid, fee_sats, script_type FROM transactions WHERE txid = ?",
            [normalized_txid],
        ).fetchone()
        if transaction_row is None:
            return None

        input_rows = self._connection.execute(
            """
            SELECT input_index, address, amount_sats
            FROM transaction_inputs
            WHERE txid = ?
            ORDER BY input_index
            """,
            [normalized_txid],
        ).fetchall()
        output_rows = self._connection.execute(
            """
            SELECT output_index, address, amount_sats
            FROM transaction_outputs
            WHERE txid = ?
            ORDER BY output_index
            """,
            [normalized_txid],
        ).fetchall()
        observation_rows = self._connection.execute(
            """
            SELECT
                observation_id,
                observed_at,
                src_ip,
                dst_ip,
                src_port,
                dst_port,
                reported_geo_country,
                reported_asn,
                source_record_id
            FROM network_observations
            WHERE txid = ?
            ORDER BY observed_at, observation_id
            """,
            [normalized_txid],
        ).fetchall()
        provenance_rows = self._connection.execute(
            """
            SELECT
                source_records.source_record_id,
                source_records.source_file,
                source_records.source_format,
                source_records.source_file_sha256,
                source_records.record_index
            FROM transaction_sources
            JOIN source_records USING (source_record_id)
            WHERE transaction_sources.txid = ?
            ORDER BY source_records.record_index, source_records.source_record_id
            """,
            [normalized_txid],
        ).fetchall()

        return TransactionDetail(
            transaction=Transaction(
                txid=str(transaction_row[0]),
                fee_sats=int(transaction_row[1]),
                script_type=_optional_str(transaction_row[2]),
            ),
            inputs=tuple(
                TransactionInput(int(row[0]), str(row[1]), int(row[2])) for row in input_rows
            ),
            outputs=tuple(
                TransactionOutput(int(row[0]), str(row[1]), int(row[2])) for row in output_rows
            ),
            observations=tuple(_network_observation(row) for row in observation_rows),
            provenance=tuple(
                TransactionProvenance(
                    source_record_id=str(row[0]),
                    source_file=str(row[1]),
                    source_format=str(row[2]),
                    source_file_sha256=str(row[3]),
                    record_index=int(row[4]),
                )
                for row in provenance_rows
            ),
        )

    def address_activity(self, address: str) -> AddressActivitySummary:
        normalized_address = _normalize_address(address)
        row = self._connection.execute(
            """
            WITH parameter AS (
                SELECT ?::VARCHAR AS address
            ),
            matching_inputs AS (
                SELECT txid, amount_sats
                FROM transaction_inputs, parameter
                WHERE transaction_inputs.address = parameter.address
            ),
            matching_outputs AS (
                SELECT txid, amount_sats
                FROM transaction_outputs, parameter
                WHERE transaction_outputs.address = parameter.address
            ),
            matching_txids AS (
                SELECT txid FROM matching_inputs
                UNION
                SELECT txid FROM matching_outputs
            ),
            input_summary AS (
                SELECT count(*) AS item_count, coalesce(sum(amount_sats), 0::HUGEINT) AS total
                FROM matching_inputs
            ),
            output_summary AS (
                SELECT count(*) AS item_count, coalesce(sum(amount_sats), 0::HUGEINT) AS total
                FROM matching_outputs
            ),
            observation_range AS (
                SELECT min(observed_at) AS first_seen, max(observed_at) AS last_seen
                FROM network_observations
                WHERE txid IN (SELECT txid FROM matching_txids)
            )
            SELECT
                (SELECT count(*) FROM matching_txids),
                input_summary.item_count,
                output_summary.item_count,
                input_summary.total,
                output_summary.total,
                observation_range.first_seen,
                observation_range.last_seen
            FROM input_summary, output_summary, observation_range
            """,
            [normalized_address],
        ).fetchone()
        if row is None:
            raise AssertionError("address aggregate unexpectedly returned no row")
        return AddressActivitySummary(
            address=normalized_address,
            transaction_count=int(row[0]),
            input_count=int(row[1]),
            output_count=int(row[2]),
            total_input_sats=int(row[3]),
            total_output_sats=int(row[4]),
            first_observed_at=_optional_datetime(row[5]),
            last_observed_at=_optional_datetime(row[6]),
        )

    def high_value_transactions(self, *, limit: int = 20) -> tuple[TransactionSummary, ...]:
        return tuple(
            _transaction_summary(row)
            for row in self._connection.execute(
                """
                SELECT
                    txid,
                    input_count,
                    output_count,
                    total_input_sats,
                    total_output_sats,
                    fee_sats,
                    network_observation_count,
                    first_observed_at,
                    last_observed_at
                FROM transaction_summary
                ORDER BY total_output_sats DESC, txid
                LIMIT ?
                """,
                [_validate_limit(limit)],
            ).fetchall()
        )

    def high_fee_transactions(self, *, limit: int = 20) -> tuple[HighFeeTransaction, ...]:
        rows = self._connection.execute(
            """
            SELECT
                txid,
                input_count,
                output_count,
                total_input_sats,
                total_output_sats,
                fee_sats,
                network_observation_count,
                first_observed_at,
                last_observed_at,
                CASE
                    WHEN total_input_sats = 0 THEN NULL
                    ELSE fee_sats::DOUBLE / total_input_sats
                END AS fee_to_input_ratio
            FROM transaction_summary
            ORDER BY fee_sats DESC, txid
            LIMIT ?
            """,
            [_validate_limit(limit)],
        ).fetchall()
        return tuple(
            HighFeeTransaction(
                summary=_transaction_summary(row[:9]),
                fee_to_input_ratio=None if row[9] is None else float(row[9]),
            )
            for row in rows
        )

    def transaction_summaries(self, *, limit: int = 100) -> tuple[TransactionSummary, ...]:
        rows = self._connection.execute(
            """
            SELECT
                txid,
                input_count,
                output_count,
                total_input_sats,
                total_output_sats,
                fee_sats,
                network_observation_count,
                first_observed_at,
                last_observed_at
            FROM transaction_summary
            ORDER BY txid
            LIMIT ?
            """,
            [_validate_limit(limit)],
        ).fetchall()
        return tuple(_transaction_summary(row) for row in rows)

    def ip_activity(self, ip: str) -> IpActivitySummary:
        normalized_ip = str(ip_address(ip.strip()))
        row = self._connection.execute(
            """
            WITH matching_roles AS (
                SELECT
                    observation_id,
                    txid,
                    observed_at,
                    'source' AS role,
                    src_port AS port,
                    reported_asn,
                    reported_geo_country
                FROM network_observations
                WHERE src_ip = ?
                UNION ALL
                SELECT
                    observation_id,
                    txid,
                    observed_at,
                    'destination' AS role,
                    dst_port AS port,
                    reported_asn,
                    reported_geo_country
                FROM network_observations
                WHERE dst_ip = ?
            )
            SELECT
                count(DISTINCT observation_id),
                count(DISTINCT txid),
                min(observed_at),
                max(observed_at),
                count(*) FILTER (WHERE role = 'source'),
                count(*) FILTER (WHERE role = 'destination'),
                list(DISTINCT port ORDER BY port),
                list(DISTINCT reported_asn ORDER BY reported_asn)
                    FILTER (WHERE reported_asn IS NOT NULL),
                list(DISTINCT reported_geo_country ORDER BY reported_geo_country)
                    FILTER (WHERE reported_geo_country IS NOT NULL)
            FROM matching_roles
            """,
            [normalized_ip, normalized_ip],
        ).fetchone()
        if row is None:
            raise AssertionError("IP aggregate unexpectedly returned no row")
        return IpActivitySummary(
            ip=normalized_ip,
            observation_count=int(row[0]),
            unique_txids=int(row[1]),
            first_observed_at=_optional_datetime(row[2]),
            last_observed_at=_optional_datetime(row[3]),
            source_role_count=int(row[4]),
            destination_role_count=int(row[5]),
            unique_ports=_int_tuple(row[6]),
            reported_asns=_int_tuple(row[7]),
            reported_countries=_str_tuple(row[8]),
        )

    def asn_activity(self, reported_asn: int) -> AsnActivitySummary:
        if isinstance(reported_asn, bool) or not 0 <= reported_asn <= 4_294_967_295:
            raise ValueError("reported ASN must be an integer from 0 through 4294967295")
        row = self._connection.execute(
            """
            WITH matching_observations AS (
                SELECT txid, observed_at, src_ip, dst_ip
                FROM network_observations
                WHERE reported_asn = ?
            ),
            matching_ips AS (
                SELECT src_ip AS ip FROM matching_observations
                UNION
                SELECT dst_ip AS ip FROM matching_observations
            )
            SELECT
                count(*),
                (SELECT count(*) FROM matching_ips),
                count(DISTINCT txid),
                min(observed_at),
                max(observed_at)
            FROM matching_observations
            """,
            [reported_asn],
        ).fetchone()
        if row is None:
            raise AssertionError("ASN aggregate unexpectedly returned no row")
        return AsnActivitySummary(
            reported_asn=reported_asn,
            observation_count=int(row[0]),
            unique_ips=int(row[1]),
            unique_txids=int(row[2]),
            first_observed_at=_optional_datetime(row[3]),
            last_observed_at=_optional_datetime(row[4]),
        )

    def temporal_activity(
        self,
        bucket: TimeBucket,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[TemporalActivity, ...]:
        try:
            bucket_expression = _TIME_BUCKET_SQL[bucket]
        except KeyError as error:
            raise ValueError("time bucket must be 'hour' or 'day'") from error
        if start is not None and end is not None and start >= end:
            raise ValueError("temporal range start must be before end")
        sql = f"""
            SELECT
                {bucket_expression} AS bucket_start,
                count(*) AS observation_count,
                count(DISTINCT txid) AS unique_txids,
                count(DISTINCT src_ip) AS unique_source_ips,
                count(DISTINCT dst_ip) AS unique_destination_ips
            FROM network_observations
            WHERE observed_at >= coalesce(?::TIMESTAMPTZ, '-infinity'::TIMESTAMPTZ)
              AND observed_at < coalesce(?::TIMESTAMPTZ, 'infinity'::TIMESTAMPTZ)
            GROUP BY bucket_start
            ORDER BY bucket_start
        """
        rows = self._connection.execute(sql, [start, end]).fetchall()
        return tuple(
            TemporalActivity(
                bucket_start=_datetime(row[0]),
                observation_count=int(row[1]),
                unique_txids=int(row[2]),
                unique_source_ips=int(row[3]),
                unique_destination_ips=int(row[4]),
            )
            for row in rows
        )


def _transaction_summary(row: tuple[Any, ...]) -> TransactionSummary:
    return TransactionSummary(
        txid=str(row[0]),
        input_count=int(row[1]),
        output_count=int(row[2]),
        total_input_sats=int(row[3]),
        total_output_sats=int(row[4]),
        fee_sats=int(row[5]),
        network_observation_count=int(row[6]),
        first_observed_at=_optional_datetime(row[7]),
        last_observed_at=_optional_datetime(row[8]),
    )


def _network_observation(row: tuple[Any, ...]) -> NetworkObservation:
    return NetworkObservation(
        observation_id=str(row[0]),
        observed_at=_datetime(row[1]),
        src_ip=str(row[2]),
        dst_ip=str(row[3]),
        src_port=int(row[4]),
        dst_port=int(row[5]),
        reported_geo_country=_optional_str(row[6]),
        reported_asn=None if row[7] is None else int(row[7]),
        source_record_id=str(row[8]),
    )


def _normalize_txid(txid: str) -> str:
    normalized = txid.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("txid must contain exactly 64 hexadecimal characters")
    return normalized


def _normalize_address(address: str) -> str:
    normalized = address.strip()
    if not normalized:
        raise ValueError("address must not be empty")
    return normalized


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= 1_000:
        raise ValueError("limit must be an integer from 1 through 1000")
    return limit


def _datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("DuckDB returned an unexpected timestamp value")
    return value


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _datetime(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _int_tuple(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    return tuple(int(item) for item in cast(list[Any], value))


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in cast(list[Any], value))
