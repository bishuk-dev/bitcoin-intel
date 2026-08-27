# Phase 1 Canonical Data Contract

This document is the authoritative contract for Phase 1 ingestion output. The canonical schema
version is `1.0.0`.

## Data flow

```text
CSV / JSON / XML
        ↓
format-specific adapter
        ↓
RawInputRecord
        ↓
shared Pydantic validation and normalization
        ↓
deterministic transaction deduplication
        ↓
seven explicit Parquet tables
        ↓
schema, count, key, and relationship read-back verification
```

Adapters only decode their source syntax. All formats pass the same field mapping into the same
validation and normalization path.

## Source record contract

Each record uses these fields:

| Source field | Required | Meaning |
| --- | --- | --- |
| `timestamp` | yes | ISO-8601 observation time with an explicit timezone |
| `src_ip`, `dst_ip` | yes | IPv4 or IPv6 address |
| `src_port`, `dst_port` | yes | Integer from 0 through 65535 |
| `txid` | yes | Exactly 64 hexadecimal characters |
| `input_addresses`, `output_addresses` | yes | Ordered arrays of address strings |
| `input_amounts`, `output_amounts` | yes | Ordered arrays of BTC decimal values |
| `fee` | yes | BTC decimal value |
| `script_type` | no | Supplied script label; blank is normalized to null |
| `geo_country` | no | Supplied two-letter country code; this is not verified GeoIP |
| `asn` | no | Supplied integer ASN from 0 through 4294967295 |

Unknown fields are rejected. Input and output address arrays may be empty, but each address array
must have exactly the same cardinality as its corresponding amount array. Ordering is preserved.

Addresses are trimmed, must be non-empty, must not contain whitespace or control characters, and
are limited to 256 characters. Phase 1 deliberately does not claim cryptographic or Bitcoin-network
address validation.

### Amounts

Source amounts are BTC decimal values represented by JSON numbers, integers, or decimal text. They
are parsed with decimal semantics and converted exactly using:

```text
1 BTC = 100000000 satoshis
```

Canonical amounts and fees are signed `int64` physical values constrained to non-negative valid
Bitcoin quantities. Values requiring rounding, including `0.000000001`, are rejected. Binary
floating-point objects passed directly to the Python API, negative values, non-finite values, and
values greater than 21 million BTC are rejected. No canonical amount column is floating point.

### Timestamps, identifiers, and network values

- Timestamps must include `Z` or an explicit UTC offset and are normalized to a timezone-aware UTC
  timestamp with microsecond precision. Timezone-naive values are rejected rather than guessed.
- TXIDs are normalized to lowercase. No blockchain lookup is performed.
- IP values use Python's `ipaddress` rules and canonical string rendering for both IPv4 and IPv6.
- Ports accept only integer values in `0..65535`; booleans and decimal fractions are rejected.
- `script_type` is trimmed and preserved without Bitcoin script interpretation.
- `geo_country` and `asn` are renamed `reported_geo_country` and `reported_asn` in canonical output
  to make their unverified source provenance explicit. No GeoIP or ASN enrichment occurs.

## Format contracts

All source files are UTF-8; a UTF-8 BOM is accepted.

### CSV

The first row is a header. It must contain every required source field, may omit optional fields,
and may not contain duplicate or unknown columns. Each array-valued cell contains exactly one JSON
array; no comma, semicolon, or pipe delimiter fallback exists.

```csv
input_addresses,input_amounts
"[""addr1"",""addr2""]","[0.5,0.25]"
```

A malformed array is a record-level rejection. A missing/invalid header, invalid UTF-8, or broken
CSV structure is a file-level failure.

### JSON

The document contains exactly one top-level JSON array of record objects. JSON Lines and trailing
content are not supported. Records are decoded incrementally, with decimal numbers parsed directly
as `Decimal` rather than binary float. A non-object array element is rejected as one record; a
structurally malformed document is a file-level failure.

### XML

The supported shape has an attribute-free `<records>` root containing direct, attribute-free
`<record>` elements. Namespaces and arbitrary layouts are not supported. Scalar fields contain text
only. Arrays use these repeated children:

```xml
<records>
  <record>
    <timestamp>2026-08-28T12:30:00Z</timestamp>
    <src_ip>192.0.2.1</src_ip>
    <dst_ip>198.51.100.2</dst_ip>
    <src_port>8333</src_port>
    <dst_port>49152</dst_port>
    <txid>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</txid>
    <input_addresses><address>addr1</address></input_addresses>
    <output_addresses><address>addr2</address></output_addresses>
    <input_amounts><amount>1</amount></input_amounts>
    <output_amounts><amount>0.9</amount></output_amounts>
    <fee>0.1</fee>
    <script_type>p2pkh</script_type>
    <geo_country>IN</geo_country>
    <asn>64512</asn>
  </record>
</records>
```

`input_addresses` and `output_addresses` repeat `<address>`; `input_amounts` and `output_amounts`
repeat `<amount>`. Parsing uses `defusedxml`; entity expansion and external entity processing are
not enabled. Unsafe or structurally malformed XML is a file-level failure.

## Canonical tables

Every table has an explicit Arrow and Polars schema. Non-null means a Parquet null is forbidden.

### `transactions`

One row per unique normalized TXID.

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `txid` | string | no | Primary identity; lowercase 64-character hex |
| `fee_sats` | int64 | no | Non-negative integer satoshis |
| `script_type` | string | yes | Trimmed supplied value; blank becomes null |

### `transaction_inputs`

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `txid` | string | no | References `transactions.txid` |
| `input_index` | int64 | no | Zero-based original array position |
| `address` | string | no | Normalized source address text |
| `amount_sats` | int64 | no | Non-negative integer satoshis |

`(txid, input_index)` is unique.

### `transaction_outputs`

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `txid` | string | no | References `transactions.txid` |
| `output_index` | int64 | no | Zero-based original array position |
| `address` | string | no | Normalized source address text |
| `amount_sats` | int64 | no | Non-negative integer satoshis |

`(txid, output_index)` is unique.

### `network_observations`

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `observation_id` | string | no | Deterministic SHA-256 identity; unique |
| `txid` | string | no | References `transactions.txid` |
| `observed_at` | timestamp[us, UTC] | no | Timezone-aware UTC |
| `src_ip`, `dst_ip` | string | no | Canonical IPv4/IPv6 text |
| `src_port`, `dst_port` | int64 | no | `0..65535` |
| `reported_geo_country` | string | yes | Normalized source-reported country code |
| `reported_asn` | int64 | yes | Source-reported ASN |
| `source_record_id` | string | no | References `source_records` |

### `transaction_sources`

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `txid` | string | no | References `transactions.txid` |
| `source_record_id` | string | no | References `source_records` |

`(txid, source_record_id)` is unique. Rejected records do not receive this link.

### `source_records`

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `source_record_id` | string | no | Deterministic SHA-256 identity; unique |
| `source_file` | string | no | Basename only, avoiding host-specific path leakage |
| `source_format` | string | no | `csv`, `json`, or `xml` |
| `source_file_sha256` | string | no | SHA-256 of the exact source bytes |
| `record_index` | int64 | no | Zero-based position in the source |

Every parsed source entry receives provenance, whether it is accepted or rejected.

### `rejected_records`

| Field | Type | Null | Constraint |
| --- | --- | --- | --- |
| `source_record_id` | string | no | References `source_records` |
| `source_file` | string | no | Source basename |
| `record_index` | int64 | no | Zero-based source position |
| `error_code` | string | no | Stable machine-readable code |
| `error_message` | string | no | Safe diagnostic, capped at 512 characters |
| `field_name` | string | yes | Source field associated with the failure |

Stable codes include `INVALID_TXID`, `INVALID_TIMESTAMP`, `INVALID_IP`, `INVALID_PORT`,
`INVALID_AMOUNT`, `AMOUNT_PRECISION_EXCEEDED`, `NEGATIVE_AMOUNT`, `INVALID_ADDRESS`,
`INVALID_COUNTRY`, `INVALID_ASN`, `INPUT_CARDINALITY_MISMATCH`,
`OUTPUT_CARDINALITY_MISMATCH`, `MALFORMED_ARRAY`, `MALFORMED_SOURCE_RECORD`, and
`TXID_CONTENT_CONFLICT`.

## Identity, deduplication, and conflict handling

IDs are lowercase hexadecimal SHA-256 strings calculated from UTF-8 text:

```text
source_record_id = sha256("source-record:" + source_file_sha256 + ":" + record_index)
observation_id   = sha256("network-observation:" + source_record_id)
```

No random UUID or ingestion-time timestamp participates in canonical identity.

The first accepted occurrence of a TXID establishes its blockchain definition: ordered normalized
inputs, ordered normalized outputs, fee, and nullable script type. An exact repeat reuses the one
transaction while retaining its independent provenance and network observation. A later record with
the same TXID but different blockchain content is rejected with `TXID_CONTENT_CONFLICT`; it cannot
silently overwrite the retained definition.

## Dataset publication and manifest

Output uses one deterministic, sorted `part-00000.parquet` per table and Zstandard compression:

```text
dataset/
├── manifest.json
├── transactions/part-00000.parquet
├── transaction_inputs/part-00000.parquet
├── transaction_outputs/part-00000.parquet
├── network_observations/part-00000.parquet
├── transaction_sources/part-00000.parquet
├── source_records/part-00000.parquet
└── rejected_records/part-00000.parquet
```

The manifest records schema version, source basename/format/hash/size, read/accepted/rejected counts,
unique transaction and observation counts, and each output table's path, row count, byte size, and
SHA-256. Critical row provenance remains in Parquet rather than only in the manifest.

Before publication, every Parquet file is read back and checked against its explicit schema, expected
row count, key uniqueness, foreign-key-like relationships, and the prohibition on floating columns.
Files are written under a sibling staging directory and published by same-filesystem rename only after
verification. Existing outputs fail safely and are never silently overwritten. Failed staging output
is removed.

Record validation failures are quarantined while parsing continues. Document-level syntax errors,
unsupported extensions, unreadable or concurrently changed inputs, schema/write/read-back failures,
and existing destinations fail the complete ingestion and publish no dataset.

## Versioning and current limits

Future incompatible table or semantic changes require a schema major-version increase. Backward-
compatible additions require a minor version; clarifications or compatible fixes use a patch version.
Readers should reject unsupported major versions.

Phase 1 deliberately uses a single predictable Parquet part per table. Adapters read incrementally,
but canonical rows and TXID definitions are retained in memory for the run, giving expected `O(n)`
time and memory. External-memory deduplication and workload-informed partitioning belong to a later
phase after representative scale measurements. DuckDB, graph storage, GeoIP enrichment, analytics,
ML, risk scoring, and API uploads are not part of this contract.
