# Offline IP Enrichment Contract

Phase 6 derives country and ASN intelligence from explicitly supplied local MMDB files. Canonical
Parquet remains the source of truth. The enrichment directory is external-data-derived state: it
can be deleted and rebuilt without changing a canonical observation.

## Provider and offline resources

The default supported resources are DB-IP IP to Country Lite and IP to ASN Lite in MMDB format.
The application uses the provider-neutral `maxminddb` reader and never calls DB-IP APIs, checks for
updates, or downloads data. Stage the licensed files outside Git, conventionally as:

```text
resources/geoip/dbip-country-lite.mmdb
resources/geoip/dbip-asn-lite.mmdb
```

Both paths are explicit CLI inputs. Each file must exist, be non-empty, parse as MMDB, and declare
a database type compatible with its country or ASN role. The build opens each reader once and
reuses it for all distinct canonical endpoints.

DB-IP Lite data is external data licensed under CC BY 4.0. Required attribution: **IP Geolocation
by DB-IP** — <https://db-ip.com>. See [`../THIRD_PARTY_DATA.md`](../THIRD_PARTY_DATA.md).

## Output and identity

```text
enrichment-manifest.json
ip_enrichment/part-00000.parquet
```

`enrichment_schema_version` is `1.0.0`. Rows are sorted by canonical IP, compressed with ZSTD, and
published only after staged validation. An existing destination is never overwritten. Semantic
`enrichment_dataset_id` depends on the canonical manifest/schema, enrichment schema and
configuration, and complete country/ASN resource metadata including SHA-256. `built_at` is
operational metadata and is excluded from identity.

The manifest records provider, role, MMDB database type, release when known, MMDB build epoch, IP
version, filename, byte size, SHA-256, license, attribution, and provider URL. Filename alone is
never treated as resource identity.

## Schema

| Field | Type | Meaning |
| --- | --- | --- |
| `ip` | string, required | Canonical IPv4/IPv6 endpoint; one row per distinct canonical IP |
| `enriched_country_code` | string, nullable | ISO 3166-1 alpha-2 country code from Country MMDB |
| `enriched_asn` | int64, nullable | Positive AS number from ASN MMDB |
| `enriched_as_org` | string, nullable | Descriptive AS organization; never an identifier |
| `country_found` | bool | Whether a valid country code was found |
| `asn_found` | bool | Whether an ASN or AS organization was found |
| `country_network` | string, nullable | Country MMDB prefix that matched the IP |
| `asn_network` | string, nullable | ASN MMDB prefix that matched the IP |
| `is_private` | bool | Python `ipaddress.is_private` result |
| `is_global` | bool | Python `ipaddress.is_global` result |
| `is_loopback` | bool | Python `ipaddress.is_loopback` result |
| `is_link_local` | bool | Python `ipaddress.is_link_local` result |
| `is_multicast` | bool | Python `ipaddress.is_multicast` result |
| `is_reserved` | bool | Python `ipaddress.is_reserved` result |

An MMDB miss is valid and emits `NULL` values with false found flags. Non-global is not renamed
"private"; the standard-library classifications remain separate. IPv4 and IPv6 use the same
pipeline.

## Reported versus enriched values

Canonical `reported_geo_country` and `reported_asn` are source-supplied observation metadata.
`enriched_country_code`, `enriched_asn`, and `enriched_as_org` are endpoint-specific DB-IP-derived
facts. Enrichment never updates canonical Parquet. Because reported fields do not define which
endpoint they describe, Feature v2 does not compare them with source/destination enrichment.

## CLI

```bash
uv run bitcoin-intel enrichment build \
  --dataset ./dataset \
  --country-db ../../resources/geoip/dbip-country-lite.mmdb \
  --asn-db ../../resources/geoip/dbip-asn-lite.mmdb \
  --output ./enrichment

uv run bitcoin-intel enrichment validate \
  --dataset ./dataset --enrichment ./enrichment
```

Validation checks lineage and semantic identity, schema/count/size/hash metadata, exact canonical
endpoint cardinality, unique and canonical IPs, country/ASN values, matched networks, found flags,
and address classifications.

## Limitations

Country and ASN enrichment is approximate external intelligence and can be missing, stale, or
incorrect. A country is not a precise physical location. An IP, country, ASN, or AS organization
does not identify a person, device, wallet, or transaction originator. City, coordinates, postal
codes, and street-level claims are intentionally out of scope.
