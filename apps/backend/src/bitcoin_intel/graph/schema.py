from __future__ import annotations

from neo4j import Driver

from bitcoin_intel.graph.constants import CONSTRAINTS, EXPECTED_CONSTRAINT_NAMES
from bitcoin_intel.graph.models import PluginVersions


def ensure_graph_constraints(driver: Driver, database: str) -> tuple[str, ...]:
    with driver.session(database=database) as session:
        for _, statement in CONSTRAINTS:
            session.run(statement).consume()
    return graph_constraint_names(driver, database)


def graph_constraint_names(driver: Driver, database: str) -> tuple[str, ...]:
    with driver.session(database=database) as session:
        records = session.run(
            "SHOW CONSTRAINTS YIELD name WHERE name IN $names RETURN name ORDER BY name",
            names=sorted(EXPECTED_CONSTRAINT_NAMES),
        )
        return tuple(str(record["name"]) for record in records)


def plugin_versions(driver: Driver, database: str) -> PluginVersions:
    with driver.session(database=database) as session:
        component = session.run(
            """CALL dbms.components() YIELD name, versions, edition
            WHERE name = 'Neo4j Kernel'
            RETURN versions[0] AS version, edition"""
        ).single(strict=True)
        gds = session.run("RETURN gds.version() AS version").single(strict=True)
        apoc = session.run("RETURN apoc.version() AS version").single(strict=True)
    return PluginVersions(
        neo4j=str(component["version"]),
        edition=str(component["edition"]),
        gds=str(gds["version"]),
        apoc=str(apoc["version"]),
    )
