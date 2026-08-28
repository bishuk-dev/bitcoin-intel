# Neo4j Offline Deployment

## Pinned runtime

| Component | Exact version |
|---|---:|
| Neo4j Community | `2026.07.1` |
| Graph Data Science Community | `2026.07.0` |
| APOC Core | `2026.07.1` |
| Neo4j Python driver | `6.2.0` |

The Dockerfile pins the base image by both version and digest. GDS and APOC Core are copied from the
matching artifact directories already bundled in that distribution into `/var/lib/neo4j/plugins`;
there is no `NEO4J_PLUGINS` download or runtime package installation. Compatibility was checked
against the official [GDS compatibility matrix](https://neo4j.com/docs/graph-data-science/current/installation/supported-neo4j-versions/),
[APOC installation documentation](https://neo4j.com/docs/apoc/current/installation/), and
[Neo4j Docker plugin guidance](https://neo4j.com/docs/operations-manual/current/docker/plugins/).

The image also contains the offline-safe settings: UTC temporal defaults, GDS/APOC procedure policy,
anonymous usage reporting disabled, Fleet Manager disabled, Fleet discovery disabled, and forwarded
HTTP headers disabled. Credentials, datasets, and mutable database files are not baked into it.

## Build and inspect

The connected packaging workstation needs the pinned base image once:

```bash
docker compose build neo4j
docker run --rm --entrypoint sh sih26146-neo4j:2026.07.1 \
  -c 'cat /var/lib/neo4j/PHASE3_VERSIONS && ls -1 /var/lib/neo4j/plugins'
```

Expected plugin files are `neo4j-graph-data-science-2026.07.0.jar` and
`apoc-2026.07.1-core.jar`.

## Credentials, storage, network, and memory

Copy `.env.example` to `.env`, set a nontrivial `NEO4J_PASSWORD`, and keep that file out of Git.
Blank passwords fail safely; `NEO4J_AUTH=none` is not used. Compose persists `/data` and `/logs` in
separate named volumes. Host ports 7474 and 7687 bind only to `127.0.0.1`; containers on the Compose
network can use the service name `neo4j`.

Development defaults are 512 MiB initial heap, 1 GiB maximum heap, and 512 MiB page cache. Override
`NEO4J_HEAP_INITIAL_SIZE`, `NEO4J_HEAP_MAX_SIZE`, and `NEO4J_PAGECACHE_SIZE` based on measured load.
GDS projections consume additional off-heap memory, so estimate before projection and do not assign
most of a 12 GiB workstation to Neo4j.

## Build a graph explicitly

From `apps/backend`:

```bash
uv run bitcoin-intel graph prepare --dataset ./dataset --output ./graph-import
uv run bitcoin-intel graph validate-import \
  --input ./graph-import --dataset ./dataset

uv run bitcoin-intel graph rebuild \
  --dataset ./dataset \
  --output ./graph-import-for-rebuild \
  --compose-file ../../docker-compose.yml \
  --confirm-replace-database
```

The last command is destructive: it replaces the configured Neo4j database. For an isolated test,
add a fresh `--compose-project-name` so it receives independent named volumes. Output directories
must not exist and are never overwritten. Constraints and canonical integrity checks run after each
successful import.

## Save and transfer

The reproducible exporter refuses to overwrite an existing archive and writes SHA-256 metadata:

```bash
cd apps/backend
uv run python scripts/export_neo4j_image.py
```

This creates a gitignored archive similar to:

```text
offline/neo4j/sih26146-neo4j-2026.07.1.tar
offline/neo4j/sih26146-neo4j-2026.07.1.json
```

Copy both files to the air-gapped host, verify the recorded SHA-256, then load and start locally:

```bash
docker load --input offline/neo4j/sih26146-neo4j-2026.07.1.tar
docker compose up -d neo4j
docker compose ps neo4j
```

No plugin download occurs during startup. Verify the actual runtime, not only JAR presence:

```bash
cd apps/backend
uv run bitcoin-intel graph health
uv run bitcoin-intel graph gds-verify
```

`graph health` executes `dbms.components()`, `gds.version()`, and `apoc.version()`. `gds-verify`
estimates a controlled projection and runs WCC. The Phase 3 offline acceptance test additionally
removed a disposable image tag, loaded it from its tar, attached the container to an internal-only
Docker network, waited for health, and connected through the official Python driver.
