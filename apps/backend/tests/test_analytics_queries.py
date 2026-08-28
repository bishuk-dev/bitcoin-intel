from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bitcoin_intel.analytics import AnalyticalDataset, AnalyticalQueries


def test_transaction_summary_avoids_one_to_many_join_multiplication(
    analytical_dataset_path: Path,
) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        summaries = AnalyticalQueries(connection).transaction_summaries()

    by_txid = {summary.txid: summary for summary in summaries}
    transaction_a = by_txid["a" * 64]
    assert transaction_a.input_count == 2
    assert transaction_a.output_count == 2
    assert transaction_a.total_input_sats == 300_000_000
    assert transaction_a.total_output_sats == 290_000_000
    assert transaction_a.network_observation_count == 2
    assert transaction_a.first_observed_at == datetime(2026, 8, 28, 23, 30, tzinfo=UTC)
    assert transaction_a.last_observed_at == datetime(2026, 8, 29, 0, 15, tzinfo=UTC)


def test_transaction_lookup_returns_ordered_relations_and_provenance(
    analytical_dataset_path: Path,
) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        queries = AnalyticalQueries(connection)
        detail = queries.transaction("A" * 64)
        missing = queries.transaction("f" * 64)

    assert detail is not None
    assert detail.transaction.txid == "a" * 64
    assert [item.input_index for item in detail.inputs] == [0, 1]
    assert [item.address for item in detail.inputs] == ["InputOnly", "SharedMulti"]
    assert [item.output_index for item in detail.outputs] == [0, 1]
    assert len(detail.observations) == 2
    assert len(detail.provenance) == 2
    assert [item.record_index for item in detail.provenance] == [0, 1]
    assert missing is None


def test_address_activity_handles_input_output_both_and_multiple_transactions(
    analytical_dataset_path: Path,
) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        queries = AnalyticalQueries(connection)
        input_only = queries.address_activity("InputOnly")
        output_only = queries.address_activity("OutputOnly")
        both = queries.address_activity("BothAddress")
        shared = queries.address_activity("SharedMulti")

    assert (input_only.input_count, input_only.output_count) == (1, 0)
    assert input_only.total_input_sats == 100_000_000
    assert (output_only.input_count, output_only.output_count) == (0, 1)
    assert output_only.total_output_sats == 50_000_000
    assert both.transaction_count == 2
    assert (both.input_count, both.output_count) == (1, 2)
    assert (both.total_input_sats, both.total_output_sats) == (100_000_000, 290_000_000)
    assert shared.transaction_count == 3
    assert shared.input_count == 3
    assert shared.total_input_sats == 1_000_000_000
    assert shared.first_observed_at == datetime(2026, 8, 28, 23, 30, tzinfo=UTC)
    assert shared.last_observed_at == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_query_values_are_parameterized(analytical_dataset_path: Path) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        queries = AnalyticalQueries(connection)
        result = queries.address_activity("BothAddress' OR 1=1 --")
        transaction_count = connection.execute("SELECT count(*) FROM transactions").fetchone()

    assert result.transaction_count == 0
    assert transaction_count == (3,)


def test_high_value_and_high_fee_rankings_use_documented_metrics(
    analytical_dataset_path: Path,
) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        queries = AnalyticalQueries(connection)
        high_value = queries.high_value_transactions(limit=3)
        high_fee = queries.high_fee_transactions(limit=3)

    assert [item.txid for item in high_value] == ["c" * 64, "b" * 64, "a" * 64]
    assert [item.total_output_sats for item in high_value] == [
        480_000_000,
        390_000_000,
        290_000_000,
    ]
    assert [item.summary.txid for item in high_fee] == ["c" * 64, "a" * 64, "b" * 64]
    assert high_fee[0].fee_to_input_ratio == 0.04


def test_ip_and_asn_activity_preserve_roles_and_reported_metadata(
    analytical_dataset_path: Path,
) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        queries = AnalyticalQueries(connection)
        ipv4 = queries.ip_activity("192.0.2.1")
        ipv6 = queries.ip_activity("2001:0db8:0:0:0:0:0:1")
        asn = queries.asn_activity(64512)

    assert ipv4.ip == "192.0.2.1"
    assert (ipv4.observation_count, ipv4.unique_txids) == (3, 2)
    assert (ipv4.source_role_count, ipv4.destination_role_count) == (2, 1)
    assert ipv4.unique_ports == (8333,)
    assert ipv4.reported_asns == (64512,)
    assert ipv4.reported_countries == ("IN", "US")
    assert ipv6.ip == "2001:db8::1"
    assert ipv6.destination_role_count == 2
    assert ipv6.unique_ports == (8333, 49152)
    assert (asn.observation_count, asn.unique_ips, asn.unique_txids) == (2, 3, 2)


def test_temporal_activity_uses_utc_buckets_and_ranges(
    analytical_dataset_path: Path,
) -> None:
    with AnalyticalDataset(analytical_dataset_path).connect() as connection:
        queries = AnalyticalQueries(connection)
        daily = queries.temporal_activity("day")
        empty = queries.temporal_activity(
            "hour",
            start=datetime(2027, 1, 1, tzinfo=UTC),
            end=datetime(2027, 1, 2, tzinfo=UTC),
        )

    assert [item.bucket_start for item in daily] == [
        datetime(2026, 8, 28, tzinfo=UTC),
        datetime(2026, 8, 29, tzinfo=UTC),
        datetime(2026, 8, 30, tzinfo=UTC),
    ]
    assert [item.observation_count for item in daily] == [1, 2, 1]
    assert empty == ()
