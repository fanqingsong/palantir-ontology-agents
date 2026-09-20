"""Tests for store factory, shared instance, and optional database backends."""

from __future__ import annotations

import os

import pytest

from src.ontology.factory import backend_mode, create_store, get_shared_store, reset_shared_store
from src.ontology.schema import Organization, Relationship, RelationshipType
from src.ontology.store import OntologyStore


def test_default_backend_is_memory(monkeypatch):
    monkeypatch.delenv("ONTOLOGY_BACKEND", raising=False)
    assert backend_mode() == "memory"
    store = create_store()
    assert store.entity_count == 0
    store.add_entity(Organization(id="x", name="X"))
    assert store.get_entity("x").name == "X"


def test_shared_store_is_reused_and_seeded():
    first = get_shared_store()
    second = get_shared_store()
    assert first is second
    assert first.get_entity("tsmc") is not None
    assert first.relationship_count >= 100


def test_shared_store_reset_creates_new_instance():
    original = get_shared_store()
    reset_shared_store()
    replacement = get_shared_store()
    assert replacement is not original
    assert replacement.get_entity("tsmc") is not None


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_postgres_backend_roundtrip(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    reset_shared_store()
    store = create_store()
    store.add_entity(Organization(id="pg_org", name="PG Org", country="US"))
    assert store.get_entity("pg_org").name == "PG Org"


@pytest.mark.skipif(not os.environ.get("TEST_NEO4J_URI"), reason="TEST_NEO4J_URI not set")
def test_neo4j_backend_path(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_BACKEND", "neo4j")
    monkeypatch.setenv("NEO4J_URI", os.environ["TEST_NEO4J_URI"])
    monkeypatch.setenv("NEO4J_USER", os.environ.get("NEO4J_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.environ.get("NEO4J_PASSWORD", "ontology-dev"))
    reset_shared_store()
    store = create_store()
    store.add_entity(Organization(id="a", name="A"))
    store.add_entity(Organization(id="b", name="B"))
    store.add_relationship(Relationship(
        id="ab", source_id="a", target_id="b",
        relationship_type=RelationshipType.RELATED_TO,
    ))
    assert store.find_path("a", "b") == ["a", "b"]


def test_snapshot_stats_are_lightweight(minimal_store: OntologyStore):
    stats = minimal_store.snapshot_stats()
    assert stats["entity_count"] == 3
    assert "entities" not in stats


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("TEST_NEO4J_URI"),
    reason="dual integration services not set",
)
def test_dual_alias_projection_and_readonly_search():
    from src.ontology.dual import DualBackend
    from src.ontology.neo4j_backend import Neo4jBackend
    from src.ontology.postgres_backend import PostgresBackend

    postgres = PostgresBackend(os.environ["TEST_DATABASE_URL"], record_outbox=True)
    neo4j = Neo4jBackend(
        os.environ["TEST_NEO4J_URI"],
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "ontology-dev"),
    )
    store = OntologyStore(DualBackend(postgres, neo4j))
    try:
        store.add_entity(Organization(
            id="dual_link_test",
            name="Dual Link Test",
            aliases=["双写链接测试"],
        ))
        store.add_alias("dual_link_test", "Dual Alias", verified=True)
        store.drain_outbox()
        candidates = neo4j.search_entity_candidates("Dual Alias", ["organization"], 10)
        assert any(item["entity"].id == "dual_link_test" for item in candidates)
        result = neo4j.execute_readonly(
            "MATCH (n:Entity) WHERE n.id = $id RETURN n.id AS id LIMIT $limit",
            {"id": "dual_link_test", "limit": 5},
        )
        assert result["rows"] == [{"id": "dual_link_test"}]
        assert result["audit"]["cypher_hash"]
    finally:
        store._backend.close()
