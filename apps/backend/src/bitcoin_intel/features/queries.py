from __future__ import annotations

TRANSACTION_FEATURE_QUERY = r"""
WITH input_stats AS (
    SELECT txid, count(*)::BIGINT AS input_count,
           sum(amount_sats)::BIGINT AS total_input_sats,
           avg(amount_sats)::DOUBLE AS mean_input_sats,
           max(amount_sats)::BIGINT AS max_input_sats,
           min(amount_sats)::BIGINT AS min_input_sats,
           stddev_samp(amount_sats)::DOUBLE AS input_value_std
    FROM scoped_inputs GROUP BY txid
),
output_stats AS (
    SELECT txid, count(*)::BIGINT AS output_count,
           sum(amount_sats)::BIGINT AS total_output_sats,
           avg(amount_sats)::DOUBLE AS mean_output_sats,
           max(amount_sats)::BIGINT AS max_output_sats,
           min(amount_sats)::BIGINT AS min_output_sats,
           stddev_samp(amount_sats)::DOUBLE AS output_value_std
    FROM scoped_outputs GROUP BY txid
),
observation_ips AS (
    SELECT txid, src_ip AS ip FROM scoped_observations
    UNION ALL
    SELECT txid, dst_ip AS ip FROM scoped_observations
),
ordered_observations AS (
    SELECT *, lag(observed_at) OVER (
        PARTITION BY txid ORDER BY observed_at, observation_id
    ) AS previous_observed_at
    FROM scoped_observations
),
observation_stats AS (
    SELECT txid,
           count(*)::BIGINT AS network_observation_count,
           count(DISTINCT src_ip)::BIGINT AS unique_source_ip_count,
           count(DISTINCT dst_ip)::BIGINT AS unique_destination_ip_count,
           count(DISTINCT reported_asn)::BIGINT AS unique_reported_asn_count,
           count(DISTINCT reported_geo_country)::BIGINT AS unique_reported_country_count,
           min(observed_at) AS first_observed_at,
           max(observed_at) AS last_observed_at,
           date_diff('second', min(observed_at), max(observed_at))::BIGINT
               AS observation_span_seconds,
           avg(epoch(observed_at - previous_observed_at))::DOUBLE
               AS mean_inter_observation_seconds,
           median(epoch(observed_at - previous_observed_at))::DOUBLE
               AS median_inter_observation_seconds,
           min(epoch(observed_at - previous_observed_at))::BIGINT
               AS min_inter_observation_seconds,
           max(epoch(observed_at - previous_observed_at))::BIGINT
               AS max_inter_observation_seconds,
           count(DISTINCT date_trunc('hour', observed_at))::BIGINT AS active_hour_count
    FROM ordered_observations GROUP BY txid
),
unique_ips AS (
    SELECT txid, count(DISTINCT ip)::BIGINT AS unique_ip_count
    FROM observation_ips GROUP BY txid
),
hour_buckets AS (
    SELECT txid, extract(hour FROM observed_at)::INTEGER AS bucket, count(*)::DOUBLE AS n
    FROM scoped_observations GROUP BY txid, bucket
),
hour_entropy AS (
    SELECT txid, -sum((n / sum_n) * ln(n / sum_n))::DOUBLE AS hour_of_day_entropy
    FROM (SELECT *, sum(n) OVER (PARTITION BY txid) AS sum_n FROM hour_buckets)
    GROUP BY txid
),
day_buckets AS (
    SELECT txid, date_trunc('day', observed_at) AS bucket, count(*)::DOUBLE AS n
    FROM scoped_observations GROUP BY txid, bucket
),
day_entropy AS (
    SELECT txid, -sum((n / sum_n) * ln(n / sum_n))::DOUBLE AS day_activity_entropy
    FROM (SELECT *, sum(n) OVER (PARTITION BY txid) AS sum_n FROM day_buckets)
    GROUP BY txid
),
burst_rows AS (
    SELECT txid,
           count(*) OVER (PARTITION BY txid ORDER BY observed_at
               RANGE BETWEEN INTERVAL 1 MINUTE PRECEDING AND CURRENT ROW)::BIGINT AS n1,
           count(*) OVER (PARTITION BY txid ORDER BY observed_at
               RANGE BETWEEN INTERVAL 5 MINUTE PRECEDING AND CURRENT ROW)::BIGINT AS n5,
           count(*) OVER (PARTITION BY txid ORDER BY observed_at
               RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW)::BIGINT AS n60
    FROM scoped_observations
),
bursts AS (
    SELECT txid, max(n1)::BIGINT AS max_observations_1m,
           max(n5)::BIGINT AS max_observations_5m,
           max(n60)::BIGINT AS max_observations_1h
    FROM burst_rows GROUP BY txid
)
SELECT t.txid,
       coalesce(i.input_count, 0)::BIGINT AS input_count,
       coalesce(o.output_count, 0)::BIGINT AS output_count,
       coalesce(i.total_input_sats, 0)::BIGINT AS total_input_sats,
       coalesce(o.total_output_sats, 0)::BIGINT AS total_output_sats,
       t.fee_sats::BIGINT AS fee_sats,
       i.mean_input_sats, o.mean_output_sats,
       i.max_input_sats, o.max_output_sats, i.min_input_sats, o.min_output_sats,
       i.input_value_std, o.output_value_std,
       CASE WHEN coalesce(i.total_input_sats, 0) = 0 THEN NULL
            ELSE t.fee_sats::DOUBLE / i.total_input_sats END AS fee_to_input_ratio,
       coalesce(s.network_observation_count, 0)::BIGINT AS network_observation_count,
       coalesce(s.unique_source_ip_count, 0)::BIGINT AS unique_source_ip_count,
       coalesce(s.unique_destination_ip_count, 0)::BIGINT AS unique_destination_ip_count,
       coalesce(u.unique_ip_count, 0)::BIGINT AS unique_ip_count,
       coalesce(s.unique_reported_asn_count, 0)::BIGINT AS unique_reported_asn_count,
       coalesce(s.unique_reported_country_count, 0)::BIGINT AS unique_reported_country_count,
       s.first_observed_at, s.last_observed_at, s.observation_span_seconds,
       s.mean_inter_observation_seconds, s.median_inter_observation_seconds,
       s.min_inter_observation_seconds, s.max_inter_observation_seconds,
       coalesce(s.active_hour_count, 0)::BIGINT AS active_hour_count,
       CASE WHEN coalesce(s.active_hour_count, 0) = 0 THEN NULL
            ELSE s.network_observation_count::DOUBLE / s.active_hour_count END
            AS observations_per_active_hour,
       h.hour_of_day_entropy, d.day_activity_entropy,
       coalesce(b.max_observations_1m, 0)::BIGINT AS max_observations_1m,
       coalesce(b.max_observations_5m, 0)::BIGINT AS max_observations_5m,
       coalesce(b.max_observations_1h, 0)::BIGINT AS max_observations_1h
FROM scoped_transactions t
LEFT JOIN input_stats i USING (txid)
LEFT JOIN output_stats o USING (txid)
LEFT JOIN observation_stats s USING (txid)
LEFT JOIN unique_ips u USING (txid)
LEFT JOIN hour_entropy h USING (txid)
LEFT JOIN day_entropy d USING (txid)
LEFT JOIN bursts b USING (txid)
ORDER BY t.txid
"""

ADDRESS_FEATURE_QUERY = r"""
WITH input_stats AS (
    SELECT address, count(*)::BIGINT AS input_occurrence_count,
           count(DISTINCT txid)::BIGINT AS unique_input_tx_count,
           sum(amount_sats)::BIGINT AS total_input_sats,
           avg(amount_sats)::DOUBLE AS mean_input_sats,
           max(amount_sats)::BIGINT AS max_input_sats
    FROM scoped_inputs GROUP BY address
),
output_stats AS (
    SELECT address, count(*)::BIGINT AS output_occurrence_count,
           count(DISTINCT txid)::BIGINT AS unique_output_tx_count,
           sum(amount_sats)::BIGINT AS total_output_sats,
           avg(amount_sats)::DOUBLE AS mean_output_sats,
           max(amount_sats)::BIGINT AS max_output_sats
    FROM scoped_outputs GROUP BY address
),
addresses AS (SELECT address FROM address_transactions GROUP BY address),
transaction_counts AS (
    SELECT address, count(*)::BIGINT AS unique_tx_count
    FROM address_transactions GROUP BY address
),
counterparties AS (
    SELECT left_side.address, count(DISTINCT right_side.address)::BIGINT
        AS co_transaction_address_count
    FROM address_transactions left_side
    JOIN address_transactions right_side USING (txid)
    WHERE left_side.address <> right_side.address
    GROUP BY left_side.address
),
ordered_observations AS (
    SELECT *, lag(observed_at) OVER (
        PARTITION BY address ORDER BY observed_at, observation_id
    ) AS previous_observed_at
    FROM address_observations
),
observation_stats AS (
    SELECT address, count(*)::BIGINT AS network_observation_count,
           min(observed_at) AS first_observed_at, max(observed_at) AS last_observed_at,
           date_diff('second', min(observed_at), max(observed_at))::BIGINT
               AS observation_span_seconds,
           avg(epoch(observed_at - previous_observed_at))::DOUBLE
               AS mean_inter_observation_seconds,
           median(epoch(observed_at - previous_observed_at))::DOUBLE
               AS median_inter_observation_seconds,
           min(epoch(observed_at - previous_observed_at))::BIGINT
               AS min_inter_observation_seconds,
           max(epoch(observed_at - previous_observed_at))::BIGINT
               AS max_inter_observation_seconds,
           count(DISTINCT date_trunc('hour', observed_at))::BIGINT AS active_hour_count
    FROM ordered_observations GROUP BY address
)
SELECT a.address,
       coalesce(i.input_occurrence_count, 0)::BIGINT AS input_occurrence_count,
       coalesce(o.output_occurrence_count, 0)::BIGINT AS output_occurrence_count,
       coalesce(i.unique_input_tx_count, 0)::BIGINT AS unique_input_tx_count,
       coalesce(o.unique_output_tx_count, 0)::BIGINT AS unique_output_tx_count,
       tc.unique_tx_count,
       coalesce(i.total_input_sats, 0)::BIGINT AS total_input_sats,
       coalesce(o.total_output_sats, 0)::BIGINT AS total_output_sats,
       i.mean_input_sats, o.mean_output_sats, i.max_input_sats, o.max_output_sats,
       CASE WHEN coalesce(o.unique_output_tx_count, 0) = 0 THEN NULL
            ELSE coalesce(i.unique_input_tx_count, 0)::DOUBLE / o.unique_output_tx_count END
            AS input_output_tx_ratio,
       CASE WHEN coalesce(o.total_output_sats, 0) = 0 THEN NULL
            ELSE coalesce(i.total_input_sats, 0)::DOUBLE / o.total_output_sats END
            AS input_output_value_ratio,
       coalesce(c.co_transaction_address_count, 0)::BIGINT AS co_transaction_address_count,
       coalesce(s.network_observation_count, 0)::BIGINT AS network_observation_count,
       s.first_observed_at, s.last_observed_at, s.observation_span_seconds,
       s.mean_inter_observation_seconds, s.median_inter_observation_seconds,
       s.min_inter_observation_seconds, s.max_inter_observation_seconds,
       coalesce(s.active_hour_count, 0)::BIGINT AS active_hour_count,
       CASE WHEN coalesce(s.active_hour_count, 0) = 0 THEN NULL
            ELSE s.network_observation_count::DOUBLE / s.active_hour_count END
            AS observations_per_active_hour,
       g.bipartite_component_size::BIGINT AS bipartite_component_size
FROM addresses a
JOIN transaction_counts tc USING (address)
JOIN graph_component_sizes g USING (address)
LEFT JOIN input_stats i USING (address)
LEFT JOIN output_stats o USING (address)
LEFT JOIN counterparties c USING (address)
LEFT JOIN observation_stats s USING (address)
ORDER BY a.address
"""

IP_FEATURE_QUERY = r"""
WITH ordered_observations AS (
    SELECT *, lag(observed_at) OVER (
        PARTITION BY ip ORDER BY observed_at, observation_id
    ) AS previous_observed_at
    FROM ip_observations
),
base_stats AS (
    SELECT ip,
           count(DISTINCT observation_id) FILTER (WHERE is_source)::BIGINT
               AS source_observation_count,
           count(DISTINCT observation_id) FILTER (WHERE is_destination)::BIGINT
               AS destination_observation_count,
           count(*)::BIGINT AS total_observation_count,
           count(DISTINCT txid)::BIGINT AS unique_tx_count,
           count(DISTINCT src_port) FILTER (WHERE is_source)::BIGINT
               AS unique_src_port_count,
           count(DISTINCT dst_port) FILTER (WHERE is_destination)::BIGINT
               AS unique_dst_port_count,
           p.unique_port_count,
           count(DISTINCT reported_asn)::BIGINT AS unique_reported_asn_count,
           count(DISTINCT reported_geo_country)::BIGINT AS unique_reported_country_count,
           min(observed_at) AS first_observed_at, max(observed_at) AS last_observed_at,
           date_diff('second', min(observed_at), max(observed_at))::BIGINT
               AS observation_span_seconds,
           avg(epoch(observed_at - previous_observed_at))::DOUBLE
               AS mean_inter_observation_seconds,
           median(epoch(observed_at - previous_observed_at))::DOUBLE
               AS median_inter_observation_seconds,
           min(epoch(observed_at - previous_observed_at))::BIGINT
               AS min_inter_observation_seconds,
           max(epoch(observed_at - previous_observed_at))::BIGINT
               AS max_inter_observation_seconds,
           count(DISTINCT date_trunc('hour', observed_at))::BIGINT AS active_hour_count
    FROM ordered_observations o
    JOIN (
        SELECT ip, count(DISTINCT port)::BIGINT AS unique_port_count
        FROM (
            SELECT ip, src_port AS port FROM ip_observations WHERE is_source
            UNION ALL
            SELECT ip, dst_port AS port FROM ip_observations WHERE is_destination
        ) ports
        GROUP BY ip
    ) p USING (ip)
    GROUP BY o.ip, p.unique_port_count
),
hour_buckets AS (
    SELECT ip, extract(hour FROM observed_at)::INTEGER AS bucket, count(*)::DOUBLE AS n
    FROM ip_observations GROUP BY ip, bucket
),
hour_entropy AS (
    SELECT ip, -sum((n / sum_n) * ln(n / sum_n))::DOUBLE AS hour_of_day_entropy
    FROM (SELECT *, sum(n) OVER (PARTITION BY ip) AS sum_n FROM hour_buckets) GROUP BY ip
),
day_buckets AS (
    SELECT ip, date_trunc('day', observed_at) AS bucket, count(*)::DOUBLE AS n
    FROM ip_observations GROUP BY ip, bucket
),
day_entropy AS (
    SELECT ip, -sum((n / sum_n) * ln(n / sum_n))::DOUBLE AS day_activity_entropy
    FROM (SELECT *, sum(n) OVER (PARTITION BY ip) AS sum_n FROM day_buckets) GROUP BY ip
),
burst_rows AS (
    SELECT ip,
           count(*) OVER (PARTITION BY ip ORDER BY observed_at
               RANGE BETWEEN INTERVAL 1 MINUTE PRECEDING AND CURRENT ROW)::BIGINT AS n1,
           count(*) OVER (PARTITION BY ip ORDER BY observed_at
               RANGE BETWEEN INTERVAL 5 MINUTE PRECEDING AND CURRENT ROW)::BIGINT AS n5,
           count(*) OVER (PARTITION BY ip ORDER BY observed_at
               RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW)::BIGINT AS n60
    FROM ip_observations
),
bursts AS (
    SELECT ip, max(n1)::BIGINT AS max_observations_1m,
           max(n5)::BIGINT AS max_observations_5m,
           max(n60)::BIGINT AS max_observations_1h
    FROM burst_rows GROUP BY ip
)
SELECT s.ip, s.source_observation_count, s.destination_observation_count,
       s.total_observation_count, s.unique_tx_count, s.unique_src_port_count,
       s.unique_dst_port_count, s.unique_port_count, s.unique_reported_asn_count,
       s.unique_reported_country_count, s.first_observed_at, s.last_observed_at,
       s.observation_span_seconds, s.mean_inter_observation_seconds,
       s.median_inter_observation_seconds, s.min_inter_observation_seconds,
       s.max_inter_observation_seconds, s.active_hour_count,
       s.total_observation_count::DOUBLE / s.active_hour_count AS observations_per_active_hour,
       h.hour_of_day_entropy, d.day_activity_entropy,
       b.max_observations_1m, b.max_observations_5m, b.max_observations_1h
FROM base_stats s
JOIN hour_entropy h USING (ip)
JOIN day_entropy d USING (ip)
JOIN bursts b USING (ip)
ORDER BY s.ip
"""

CORRELATION_FEATURE_QUERY = r"""
WITH role_counts AS (
    SELECT address_tx.address,
           count(DISTINCT o.observation_id)::BIGINT AS network_observation_count,
           count(DISTINCT o.src_ip)::BIGINT AS distinct_source_ip_count,
           count(DISTINCT o.dst_ip)::BIGINT AS distinct_destination_ip_count,
           count(DISTINCT o.reported_asn)::BIGINT AS unique_reported_asn_count,
           count(DISTINCT o.reported_geo_country)::BIGINT AS unique_reported_country_count
    FROM address_transactions address_tx
    JOIN scoped_observations o USING (txid)
    GROUP BY address_tx.address
),
address_ip_transactions AS (
    SELECT address_tx.address, o.src_ip AS ip, o.txid
    FROM address_transactions address_tx
    JOIN scoped_observations o USING (txid)
    UNION
    SELECT address_tx.address, o.dst_ip AS ip, o.txid
    FROM address_transactions address_tx
    JOIN scoped_observations o USING (txid)
),
per_ip AS (
    SELECT address, ip, count(DISTINCT txid)::BIGINT AS transaction_count
    FROM address_ip_transactions GROUP BY address, ip
),
ip_stats AS (
    SELECT address, count(*)::BIGINT AS distinct_associated_ip_count,
           count(*) FILTER (WHERE transaction_count >= ?)::BIGINT AS reused_ip_count,
           max(transaction_count)::BIGINT AS max_transactions_per_associated_ip,
           avg(transaction_count)::DOUBLE AS mean_transactions_per_associated_ip
    FROM per_ip GROUP BY address
),
addresses AS (SELECT address FROM address_transactions GROUP BY address)
SELECT a.address,
       coalesce(r.network_observation_count, 0)::BIGINT AS network_observation_count,
       coalesce(i.distinct_associated_ip_count, 0)::BIGINT AS distinct_associated_ip_count,
       coalesce(r.distinct_source_ip_count, 0)::BIGINT AS distinct_source_ip_count,
       coalesce(r.distinct_destination_ip_count, 0)::BIGINT AS distinct_destination_ip_count,
       coalesce(r.unique_reported_asn_count, 0)::BIGINT AS unique_reported_asn_count,
       coalesce(r.unique_reported_country_count, 0)::BIGINT AS unique_reported_country_count,
       coalesce(i.reused_ip_count, 0)::BIGINT AS reused_ip_count,
       i.max_transactions_per_associated_ip, i.mean_transactions_per_associated_ip,
       CASE WHEN coalesce(i.distinct_associated_ip_count, 0) = 0 THEN NULL
            ELSE i.reused_ip_count::DOUBLE / i.distinct_associated_ip_count END AS ip_reuse_ratio
FROM addresses a
LEFT JOIN role_counts r USING (address)
LEFT JOIN ip_stats i USING (address)
ORDER BY a.address
"""

FEATURE_QUERIES = {
    "transaction_features": TRANSACTION_FEATURE_QUERY,
    "address_features": ADDRESS_FEATURE_QUERY,
    "ip_features": IP_FEATURE_QUERY,
    "correlation_features": CORRELATION_FEATURE_QUERY,
}

TRANSACTION_FEATURE_QUERY_V2 = f"""
WITH base_features AS ({TRANSACTION_FEATURE_QUERY}),
endpoint_values AS (
    SELECT txid, src_enriched_country_code AS country_code, src_enriched_asn AS asn
    FROM enriched_observations
    UNION ALL
    SELECT txid, dst_enriched_country_code AS country_code, dst_enriched_asn AS asn
    FROM enriched_observations
),
enrichment_diversity AS (
    SELECT txid,
           count(DISTINCT country_code)::BIGINT AS unique_enriched_country_count,
           count(DISTINCT asn)::BIGINT AS unique_enriched_asn_count
    FROM endpoint_values GROUP BY txid
),
pair_rates AS (
    SELECT txid,
           CAST(avg((src_enriched_country_code = dst_enriched_country_code)::INTEGER)
               FILTER (WHERE src_enriched_country_code IS NOT NULL
                       AND dst_enriched_country_code IS NOT NULL) AS DOUBLE)
               AS source_destination_country_match_rate,
           CAST(avg((src_enriched_asn = dst_enriched_asn)::INTEGER)
               FILTER (WHERE src_enriched_asn IS NOT NULL AND dst_enriched_asn IS NOT NULL)
               AS DOUBLE)
               AS source_destination_asn_match_rate
    FROM enriched_observations GROUP BY txid
)
SELECT b.*,
       coalesce(d.unique_enriched_country_count, 0)::BIGINT
           AS unique_enriched_country_count,
       coalesce(d.unique_enriched_asn_count, 0)::BIGINT AS unique_enriched_asn_count,
       p.source_destination_country_match_rate,
       p.source_destination_asn_match_rate
FROM base_features b
LEFT JOIN enrichment_diversity d USING (txid)
LEFT JOIN pair_rates p USING (txid)
ORDER BY b.txid
"""

CORRELATION_FEATURE_QUERY_V2 = f"""
WITH base_features AS ({CORRELATION_FEATURE_QUERY}),
associated_endpoints AS (
    SELECT a.address, o.observation_id,
           o.src_enriched_country_code AS country_code, o.src_enriched_asn AS asn
    FROM address_transactions a JOIN enriched_observations o USING (txid)
    UNION ALL
    SELECT a.address, o.observation_id,
           o.dst_enriched_country_code AS country_code, o.dst_enriched_asn AS asn
    FROM address_transactions a JOIN enriched_observations o USING (txid)
),
enrichment_diversity AS (
    SELECT address,
           count(DISTINCT country_code)::BIGINT AS associated_enriched_country_count,
           count(DISTINCT asn)::BIGINT AS associated_enriched_asn_count
    FROM associated_endpoints GROUP BY address
),
cross_endpoint_counts AS (
    SELECT a.address,
           count(DISTINCT o.observation_id) FILTER (
               WHERE o.src_enriched_country_code IS NOT NULL
                 AND o.dst_enriched_country_code IS NOT NULL
                 AND o.src_enriched_country_code <> o.dst_enriched_country_code
           )::BIGINT AS associated_cross_country_observation_count,
           count(DISTINCT o.observation_id) FILTER (
               WHERE o.src_enriched_asn IS NOT NULL AND o.dst_enriched_asn IS NOT NULL
                 AND o.src_enriched_asn <> o.dst_enriched_asn
           )::BIGINT AS associated_cross_asn_observation_count
    FROM address_transactions a JOIN enriched_observations o USING (txid)
    GROUP BY a.address
)
SELECT b.*,
       coalesce(d.associated_enriched_country_count, 0)::BIGINT
           AS associated_enriched_country_count,
       coalesce(d.associated_enriched_asn_count, 0)::BIGINT
           AS associated_enriched_asn_count,
       coalesce(c.associated_cross_country_observation_count, 0)::BIGINT
           AS associated_cross_country_observation_count,
       coalesce(c.associated_cross_asn_observation_count, 0)::BIGINT
           AS associated_cross_asn_observation_count
FROM base_features b
LEFT JOIN enrichment_diversity d USING (address)
LEFT JOIN cross_endpoint_counts c USING (address)
ORDER BY b.address
"""

FEATURE_QUERIES_V1 = FEATURE_QUERIES
FEATURE_QUERIES_V2 = {
    **FEATURE_QUERIES,
    "transaction_features": TRANSACTION_FEATURE_QUERY_V2,
    "correlation_features": CORRELATION_FEATURE_QUERY_V2,
}
