"""Postgres ontology backend. System of record for entities and the dual-write outbox."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.ontology.codec import (
    entity_to_record,
    record_to_entity,
    record_to_relationship,
    relationship_to_record,
)
from src.ontology.graph_ops import (
    calculate_exposure_score,
    find_path,
    get_dependency_chains,
    store_to_dict,
    traverse,
)
from src.ontology.schema import Entity, EntityType, Relationship, RelationshipType

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class PostgresBackend:
    def __init__(self, dsn: str, record_outbox: bool = False) -> None:
        import psycopg

        self._dsn = dsn
        self.record_outbox = record_outbox
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        self.apply_schema()

    def apply_schema(self) -> None:
        with self._conn.cursor() as cur:
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                cur.execute(path.read_text(encoding="utf-8"))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _upsert_entity_row(self, entity: Entity) -> None:
        rec = entity_to_record(entity)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entities (
                    id, entity_type, name, description, tags, source, confidence, attributes, created_at, updated_at
                ) VALUES (
                    %(id)s, %(entity_type)s, %(name)s, %(description)s, %(tags)s,
                    %(source)s, %(confidence)s, %(attributes)s,
                    COALESCE(NULLIF(%(created_at)s, '')::timestamptz, NOW()), NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    entity_type = EXCLUDED.entity_type,
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    tags = EXCLUDED.tags,
                    source = EXCLUDED.source,
                    confidence = EXCLUDED.confidence,
                    attributes = EXCLUDED.attributes,
                    updated_at = NOW()
                """,
                {**rec, "attributes": Json(rec["attributes"])},
            )

    def _insert_outbox(self, op: str, payload: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outbox (op, payload) VALUES (%s, %s)",
                (op, Json(payload)),
            )

    def add_entity(self, entity: Entity) -> str:
        try:
            self._upsert_entity_row(entity)
            if self.record_outbox:
                self._insert_outbox("upsert_entity", entity.to_dict())
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return entity.id

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM entities WHERE id = %s", (entity_id,))
            row = cur.fetchone()
        return record_to_entity(row) if row else None

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM entities WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,))
            row = cur.fetchone()
        return record_to_entity(row) if row else None

    def remove_entity(self, entity_id: str) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM entities WHERE id = %s RETURNING id", (entity_id,))
                deleted = cur.fetchone()
            if deleted and self.record_outbox:
                self._insert_outbox("delete_entity", {"id": entity_id})
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return deleted is not None

    def add_relationship(self, relationship: Relationship) -> str:
        rec = relationship_to_record(relationship)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO relationships (
                        id, source_id, target_id, rel_type, weight, bidirectional,
                        description, confidence, attributes, created_at
                    ) VALUES (
                        %(id)s, %(source_id)s, %(target_id)s, %(relationship_type)s, %(weight)s,
                        %(bidirectional)s, %(description)s, %(confidence)s, %(attributes)s,
                        COALESCE(NULLIF(%(created_at)s, '')::timestamptz, NOW())
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        source_id = EXCLUDED.source_id,
                        target_id = EXCLUDED.target_id,
                        rel_type = EXCLUDED.rel_type,
                        weight = EXCLUDED.weight,
                        bidirectional = EXCLUDED.bidirectional,
                        description = EXCLUDED.description,
                        confidence = EXCLUDED.confidence,
                        attributes = EXCLUDED.attributes
                    """,
                    {**rec, "attributes": Json(rec.get("attributes") or {})},
                )
            if self.record_outbox:
                self._insert_outbox("upsert_relationship", rec)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return relationship.id

    def get_relationship(self, rel_id: str) -> Optional[Relationship]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM relationships WHERE id = %s", (rel_id,))
            row = cur.fetchone()
        return record_to_relationship(row) if row else None

    def query_by_type(self, entity_type: EntityType) -> list[Entity]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM entities WHERE entity_type = %s", (entity_type.value,))
            rows = cur.fetchall()
        return [record_to_entity(row) for row in rows]

    def query_by_attribute(self, key: str, value: Any) -> list[Entity]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM entities
                WHERE attributes ->> %s = %s
                   OR name = %s
                """,
                (key, str(value), str(value)),
            )
            rows = cur.fetchall()
        # Also match typed fields stored as native JSON types
        matches = [record_to_entity(row) for row in rows]
        seen = {e.id for e in matches}
        for entity in self.all_entities():
            if entity.id in seen:
                continue
            if entity.attributes.get(key) == value:
                matches.append(entity)
            elif hasattr(entity, key) and getattr(entity, key) == value:
                matches.append(entity)
        return matches

    def search(self, query: str) -> list[Entity]:
        pattern = f"%{query}%"
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM entities
                WHERE name ILIKE %s OR description ILIKE %s
                """,
                (pattern, pattern),
            )
            rows = cur.fetchall()
        return [record_to_entity(row) for row in rows]

    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        direction: str = "both",
    ) -> list[tuple[Entity, Relationship]]:
        clauses = []
        params: list[Any] = []
        if direction in ("outgoing", "both"):
            clauses.append("(source_id = %s OR (bidirectional AND target_id = %s))")
            params.extend([entity_id, entity_id])
        if direction in ("incoming", "both"):
            clauses.append("(target_id = %s OR (bidirectional AND source_id = %s))")
            params.extend([entity_id, entity_id])
        sql = f"SELECT * FROM relationships WHERE ({' OR '.join(clauses)})"
        if relationship_type:
            sql += " AND rel_type = %s"
            params.append(relationship_type.value)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        neighbors: list[tuple[Entity, Relationship]] = []
        for row in rows:
            rel = record_to_relationship(row)
            neighbor_id = rel.target_id if rel.source_id == entity_id else rel.source_id
            neighbor = self.get_entity(neighbor_id)
            if neighbor:
                neighbors.append((neighbor, rel))
        return neighbors

    def traverse(
        self,
        entity_id: str,
        hops: int = 2,
        relationship_type: Optional[RelationshipType] = None,
    ) -> dict[str, Any]:
        return traverse(self, entity_id, hops=hops, relationship_type=relationship_type)

    def find_path(self, source_id: str, target_id: str, max_hops: int = 5) -> Optional[list[str]]:
        return find_path(self, source_id, target_id, max_hops=max_hops)

    def get_dependency_chains(
        self,
        entity_id: str,
        rel_types: Optional[list[RelationshipType]] = None,
        max_depth: int = 5,
    ) -> list[list[str]]:
        return get_dependency_chains(self, entity_id, rel_types=rel_types, max_depth=max_depth)

    def calculate_exposure_score(
        self, entity_id: str, threat_ids: Optional[list[str]] = None
    ) -> float:
        return calculate_exposure_score(self, entity_id, threat_ids=threat_ids)

    def to_dict(self) -> dict[str, Any]:
        return store_to_dict(self)

    def load_dict(self, data: dict[str, Any]) -> None:
        from src.ontology.schema import entity_from_dict

        for payload in data.get("entities", {}).values():
            self.add_entity(entity_from_dict(payload))
        for payload in data.get("relationships", {}).values():
            self.add_relationship(Relationship.from_dict(payload))

    def pending_outbox(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM outbox WHERE status = 'pending' ORDER BY id"
            )
            return list(cur.fetchall())

    def get_outbox_row(self, outbox_id: int) -> Optional[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM outbox WHERE id = %s", (outbox_id,))
            return cur.fetchone()

    def claim_outbox(
        self, outbox_id: int, worker_id: str, lock_minutes: int = 5
    ) -> Optional[dict[str, Any]]:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbox
                    SET status = 'processing',
                        locked_by = %s,
                        locked_until = NOW() + (%s * INTERVAL '1 minute')
                    WHERE id = %s AND status = 'pending'
                    RETURNING *
                    """,
                    (worker_id, lock_minutes, outbox_id),
                )
                row = cur.fetchone()
            self._conn.commit()
            return row
        except Exception:
            self._conn.rollback()
            raise

    def release_stale_processing(self, lock_minutes: int) -> int:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbox
                    SET status = 'pending',
                        locked_by = NULL,
                        locked_until = NULL
                    WHERE status = 'processing'
                      AND locked_until IS NOT NULL
                      AND locked_until < NOW()
                    RETURNING id
                    """
                )
                released = cur.fetchall()
            self._conn.commit()
            return len(released)
        except Exception:
            self._conn.rollback()
            raise

    def list_pending_outbox_ids(self, limit: int = 500) -> list[int]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM outbox
                WHERE status = 'pending'
                ORDER BY id
                LIMIT %s
                """,
                (limit,),
            )
            return [int(row["id"]) for row in cur.fetchall()]

    def mark_outbox(
        self,
        outbox_id: int,
        status: str,
        error: Optional[str] = None,
        *,
        increment_attempts: bool = False,
    ) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbox
                    SET status = %s,
                        last_error = %s,
                        locked_by = CASE WHEN %s = 'done' THEN NULL ELSE locked_by END,
                        locked_until = CASE WHEN %s = 'done' THEN NULL ELSE locked_until END,
                        attempts = attempts + CASE WHEN %s THEN 1 ELSE 0 END,
                        processed_at = CASE WHEN %s = 'done' THEN NOW() ELSE processed_at END
                    WHERE id = %s
                    """,
                    (status, error, status, status, increment_attempts, status, outbox_id),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @property
    def entity_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM entities")
            return int(cur.fetchone()["n"])

    @property
    def relationship_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM relationships")
            return int(cur.fetchone()["n"])

    def all_entities(self) -> list[Entity]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM entities")
            return [record_to_entity(row) for row in cur.fetchall()]

    def all_relationships(self) -> list[Relationship]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM relationships")
            return [record_to_relationship(row) for row in cur.fetchall()]
