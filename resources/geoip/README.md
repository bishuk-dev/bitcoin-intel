# Offline GeoIP resources

Place licensed DB-IP Lite MMDB files in this directory and pass their paths explicitly to
`bitcoin-intel enrichment build`. MMDB files are ignored by Git and are never downloaded by the
application.

The default supported provider is DB-IP Lite country and ASN data. DB-IP Lite is licensed under
Creative Commons Attribution 4.0. Required attribution: **IP Geolocation by DB-IP** —
<https://db-ip.com>.

Do not commit downloaded database files. Record the exact release you obtained and preserve its
license alongside deployment artifacts when distributing an offline bundle.
