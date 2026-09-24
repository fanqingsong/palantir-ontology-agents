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


class _SchemaConflict(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _FakeSession:
    def __init__(self, errors: dict[str, Exception]) -> None:
        self._errors = errors
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **kwargs):
        self.statements.append(query)
        for needle, exc in self._errors.items():
            if needle in query:
                raise exc


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self):
        return self._session

    def close(self):
        pass


def _patch_neo4j_driver(monkeypatch, session: _FakeSession) -> None:
    import neo4j

    class FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth):
            return _FakeDriver(session)

    monkeypatch.setattr(neo4j, "GraphDatabase", FakeGraphDatabase)


def test_neo4j_backend_ignores_existing_fulltext_index(monkeypatch):
    from src.ontology.neo4j_backend import Neo4jBackend

    session = _FakeSession(
        {"FULLTEXT": _SchemaConflict("Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists")}
    )
    _patch_neo4j_driver(monkeypatch, session)
    backend = Neo4jBackend("bolt://example", "neo4j", "pwd")
    try:
        assert any("FULLTEXT" in stmt for stmt in session.statements)
    finally:
        backend.close()


def test_neo4j_backend_reraises_other_schema_errors(monkeypatch):
    from src.ontology.neo4j_backend import Neo4jBackend

    session = _FakeSession(
        {"FULLTEXT": _SchemaConflict("Neo.ClientError.Statement.SyntaxError")}
    )
    _patch_neo4j_driver(monkeypatch, session)
    with pytest.raises(_SchemaConflict):
        Neo4jBackend("bolt://example", "neo4j", "pwd")


class _SchemaCursor:
    def __init__(self, applied: set[str]) -> None:
        self.applied = applied
        self.statements: list[str] = []
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self._sql = sql
        if params and "INSERT INTO _schema_migrations" in sql:
            self.applied.add(params[0])

    def fetchall(self):
        if "FROM _schema_migrations" in self._sql:
            return [{"filename": name} for name in sorted(self.applied)]
        return []


class _SchemaConn:
    def __init__(self, applied: set[str]) -> None:
        self.cursor_obj = _SchemaCursor(applied)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _backend_with_fake_conn(tmp_path, monkeypatch, applied: set[str]):
    from src.ontology import postgres_backend as mod

    (tmp_path / "001_init.sql").write_text("CREATE TABLE foo();", encoding="utf-8")
    (tmp_path / "002_outbox.sql").write_text("ALTER TABLE outbox ADD COLUMN locked_by TEXT;", encoding="utf-8")
    monkeypatch.setattr(mod, "MIGRATIONS_DIR", tmp_path)
    backend = object.__new__(mod.PostgresBackend)
    backend._conn = _SchemaConn(applied)
    return backend


def test_apply_schema_skips_recorded_migrations(tmp_path, monkeypatch):
    backend = _backend_with_fake_conn(
        tmp_path, monkeypatch, {"001_init.sql", "002_outbox.sql"}
    )
    backend.apply_schema()
    sql = "\n".join(backend._conn.cursor_obj.statements)
    assert "CREATE TABLE foo();" not in sql
    assert "ALTER TABLE outbox" not in sql
    assert "pg_advisory_xact_lock" not in sql
    assert backend._conn.commits == 1


def test_apply_schema_runs_pending_migrations_once(tmp_path, monkeypatch):
    backend = _backend_with_fake_conn(tmp_path, monkeypatch, set())
    backend.apply_schema()
    sql = "\n".join(backend._conn.cursor_obj.statements)
    assert "CREATE TABLE foo();" in sql
    assert "ALTER TABLE outbox ADD COLUMN locked_by TEXT;" in sql
    assert "pg_advisory_xact_lock" in sql
    assert backend._conn.commits == 1


def test_dual_graph_reads_drain_outbox_once(monkeypatch):
    from unittest.mock import Mock

    from src.ontology.dual import DualBackend

    monkeypatch.setenv("OUTBOX_SYNC_FLUSH", "false")
    postgres = Mock()
    neo4j = Mock()
    backend = DualBackend(postgres, neo4j)
    backend._projector = Mock()
    backend._projector.drain_pending.return_value = 2

    backend.get_neighbors("tsmc")
    backend.traverse("tsmc")
    backend.find_path("tsmc", "apple")
    backend.calculate_exposure_score("tsmc")
    backend.graph_search_backend()

    assert backend._projector.drain_pending.call_count == 1
    neo4j.get_neighbors.assert_called_once()
    neo4j.verify_connectivity.assert_called_once()


def test_dual_write_invalidates_projection_until_next_read(monkeypatch):
    from unittest.mock import Mock

    from src.ontology.dual import DualBackend

    monkeypatch.setenv("OUTBOX_SYNC_FLUSH", "false")
    backend = DualBackend(Mock(), Mock())
    backend._projector = Mock()
    backend._projector.drain_pending.return_value = 1

    backend.get_neighbors("tsmc")
    backend.add_entity(Organization(id="new_event", name="New Event"))
    backend.get_neighbors("tsmc")
    backend.get_dependency_chains("tsmc")

    assert backend._projector.drain_pending.call_count == 2


def test_snapshot_stats_are_lightweight(minimal_store: OntologyStore, monkeypatch):
    def refuse_full_dump(_self):
        raise AssertionError("snapshot_stats serialized the full graph")

    monkeypatch.setattr(type(minimal_store._backend), "to_dict", refuse_full_dump)
    stats = minimal_store.snapshot_stats()
    assert stats["entity_count"] == 3
    assert stats["relationship_count"] == 2
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
