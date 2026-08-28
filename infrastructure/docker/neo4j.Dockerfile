# syntax=docker/dockerfile:1.7

FROM neo4j:2026.07.1@sha256:dbc377fb9cd8fe8dabc19d3041b197d5ca0ef8bae514cea175b8df265e5b7a76

ARG NEO4J_VERSION=2026.07.1
ARG GDS_VERSION=2026.07.0
ARG APOC_VERSION=2026.07.1

RUN set -eux; \
    test -f "/var/lib/neo4j/products/neo4j-graph-data-science-${GDS_VERSION}.jar"; \
    test -f "/var/lib/neo4j/labs/apoc-${APOC_VERSION}-core.jar"; \
    install -m 0444 \
      "/var/lib/neo4j/products/neo4j-graph-data-science-${GDS_VERSION}.jar" \
      "/var/lib/neo4j/plugins/neo4j-graph-data-science-${GDS_VERSION}.jar"; \
    install -m 0444 \
      "/var/lib/neo4j/labs/apoc-${APOC_VERSION}-core.jar" \
      "/var/lib/neo4j/plugins/apoc-${APOC_VERSION}-core.jar"; \
    printf '%s\n' \
      "neo4j=${NEO4J_VERSION}" \
      "gds=${GDS_VERSION}" \
      "apoc=${APOC_VERSION}" \
      > /var/lib/neo4j/PHASE3_VERSIONS; \
    printf '%s\n' \
      'db.temporal.timezone=UTC' \
      'dbms.usage_report.enabled=false' \
      'dbms.fleet_manager.enabled=false' \
      'server.fleet_discovery.enabled=false' \
      'server.http.x_forward.enabled=false' \
      'dbms.security.procedures.allowlist=gds.*,apoc.*' \
      'dbms.security.procedures.unrestricted=gds.*' \
      >> /var/lib/neo4j/conf/neo4j.conf

EXPOSE 7474 7687
