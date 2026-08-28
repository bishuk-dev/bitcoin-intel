from __future__ import annotations

from uuid import uuid4

from neo4j import Driver

from bitcoin_intel.graph.models import GdsVerification


def verify_gds_foundation(driver: Driver, database: str) -> GdsVerification:
    """Estimate, project and run read-only WCC over factual Address/Transaction edges."""

    graph_name = f"phase3_validation_{uuid4().hex}"
    node_projection = ["Address", "Transaction"]
    relationship_projection = {
        "SPENT_IN": {"orientation": "NATURAL"},
        "CREATED_OUTPUT": {"orientation": "NATURAL"},
    }
    with driver.session(database=database) as session:
        estimate = session.run(
            """CALL gds.graph.project.estimate($nodes, $relationships, {readConcurrency: 1})
            YIELD bytesMin, bytesMax, nodeCount, relationshipCount
            RETURN bytesMin, bytesMax, nodeCount, relationshipCount""",
            nodes=node_projection,
            relationships=relationship_projection,
        ).single(strict=True)
        projected = False
        try:
            projection = session.run(
                """CALL gds.graph.project(
                    $name, $nodes, $relationships,
                    {readConcurrency: 1, validateRelationships: true}
                )
                YIELD graphName, nodeCount, relationshipCount, projectMillis
                RETURN graphName, nodeCount, relationshipCount, projectMillis""",
                name=graph_name,
                nodes=node_projection,
                relationships=relationship_projection,
            ).single(strict=True)
            projected = True
            wcc = session.run(
                """CALL gds.wcc.stats($name, {concurrency: 1})
                YIELD componentCount, computeMillis
                RETURN componentCount, computeMillis""",
                name=graph_name,
            ).single(strict=True)
        finally:
            if projected:
                session.run(
                    "CALL gds.graph.drop($name) YIELD graphName RETURN graphName",
                    name=graph_name,
                ).consume()
    return GdsVerification(
        graph_name=str(projection["graphName"]),
        estimated_bytes_min=int(estimate["bytesMin"]),
        estimated_bytes_max=int(estimate["bytesMax"]),
        node_count=int(projection["nodeCount"]),
        relationship_count=int(projection["relationshipCount"]),
        component_count=int(wcc["componentCount"]),
        project_millis=int(projection["projectMillis"]),
        compute_millis=int(wcc["computeMillis"]),
    )
