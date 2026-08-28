from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError

from bitcoin_intel.graph.config import GraphSettings


class GraphConnectionError(RuntimeError):
    """Raised when an explicit graph operation cannot connect to Neo4j."""


class GraphConnection:
    def __init__(self, settings: GraphSettings) -> None:
        self.settings = settings

    @contextmanager
    def connect(self) -> Iterator[Driver]:
        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(
                self.settings.neo4j_user,
                self.settings.neo4j_password.get_secret_value(),
            ),
            connection_timeout=self.settings.neo4j_connection_timeout_seconds,
        )
        try:
            driver.verify_connectivity()
            yield driver
        except (DriverError, Neo4jError, OSError) as error:
            raise GraphConnectionError(f"Neo4j operation failed: {error}") from error
        finally:
            driver.close()
