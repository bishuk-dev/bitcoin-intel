from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from bitcoin_intel.graph.config import GraphSettings
from bitcoin_intel.graph.docker import (
    GraphRebuildError,
    _direct_bolt_uri,
    neo4j_admin_import_arguments,
    rebuild_graph,
)


def test_graph_settings_require_a_nontrivial_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="neo4j_password"):
        GraphSettings()  # type: ignore[call-arg]


def test_graph_settings_validate_uri_and_normalize_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
    monkeypatch.setenv("NEO4J_USER", " graph-user ")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-password")

    settings = GraphSettings()  # type: ignore[call-arg]

    assert settings.neo4j_user == "graph-user"
    assert settings.neo4j_password.get_secret_value() == "test-password"

    monkeypatch.setenv("NEO4J_URI", "http://127.0.0.1:7474")
    with pytest.raises(ValidationError, match="supported Neo4j or Bolt URI"):
        GraphSettings()  # type: ignore[call-arg]


def test_neo4j_admin_import_is_strict_and_has_explicit_id_groups() -> None:
    arguments = neo4j_admin_import_arguments(dry_run=True)
    joined = " ".join(arguments)

    assert "--input-type=parquet" in arguments
    assert "--strict=true" in arguments
    assert "--skip-duplicate-nodes=false" in arguments
    assert "--skip-bad-relationships=false" in arguments
    assert "--bad-tolerance=0" in arguments
    assert "--dry-run=true" in arguments
    assert "Transaction=" in joined
    assert "Address=" in joined
    assert "IPAddress=" in joined
    assert "NetworkObservation=" in joined
    first_relationship = next(
        index for index, value in enumerate(arguments) if value.startswith("--relationships=")
    )
    assert arguments.index("neo4j") < first_relationship


def test_rebuild_requires_explicit_destructive_confirmation(tmp_path: Path) -> None:
    settings = GraphSettings(neo4j_password=SecretStr("test-password"))

    with pytest.raises(GraphRebuildError, match="confirm-replace-database"):
        rebuild_graph(
            dataset_path=tmp_path / "not-read",
            output_path=tmp_path / "not-created",
            compose_file=tmp_path / "not-read.yml",
            settings=settings,
            confirm_replace_database=False,
        )

    assert not (tmp_path / "not-created").exists()


def test_rebuild_rejects_unsafe_compose_project_name(tmp_path: Path) -> None:
    settings = GraphSettings(neo4j_password=SecretStr("test-password"))

    with pytest.raises(ValueError, match="Compose project name"):
        rebuild_graph(
            dataset_path=tmp_path / "not-read",
            output_path=tmp_path / "not-created",
            compose_file=tmp_path / "not-read.yml",
            settings=settings,
            confirm_replace_database=True,
            compose_project_name="../../unsafe",
        )


@pytest.mark.parametrize(
    ("configured", "direct"),
    [
        ("neo4j://graph:7687", "bolt://graph:7687"),
        ("neo4j+s://graph:7687", "bolt+s://graph:7687"),
        ("neo4j+ssc://graph:7687", "bolt+ssc://graph:7687"),
        ("bolt://graph:7687", "bolt://graph:7687"),
    ],
)
def test_readiness_uri_preserves_transport_security(configured: str, direct: str) -> None:
    assert _direct_bolt_uri(configured) == direct
