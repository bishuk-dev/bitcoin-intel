from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GraphSettings(BaseSettings):
    """Neo4j connection settings loaded only by explicit graph operations."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        str_strip_whitespace=True,
    )

    neo4j_uri: str = Field(default="neo4j://127.0.0.1:7687", min_length=1)
    neo4j_user: str = Field(default="neo4j", min_length=1)
    neo4j_password: SecretStr = Field(min_length=8)
    neo4j_database: str = Field(default="neo4j", pattern=r"^[A-Za-z0-9._-]+$")
    neo4j_connection_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    def model_post_init(self, __context: object) -> None:
        allowed_prefixes = ("neo4j://", "neo4j+s://", "neo4j+ssc://", "bolt://")
        if not self.neo4j_uri.startswith(allowed_prefixes):
            raise ValueError("NEO4J_URI must use a supported Neo4j or Bolt URI scheme")
