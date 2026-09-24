"""Postgres ontology backend. System of record for entities and the dual-write outbox."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Optional

from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb

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
from src.entity_linking.embeddings import cosine_similarity
from src.entity_linking.normalizer import normalize_surface

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
# Session lock so concurrent Prefect workers do not ALTER outbox together.
_SCHEMA_ADVISORY_LOCK = 872511


class PostgresBackend:
    def __init__(self, dsn: str, record_outbox: bool = False) -> None:
        import psycopg

        self._dsn = dsn
        self.record_outbox = record_outbox
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        self.apply_schema()

    def _schema_is_current(self, files: list[Path]) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT filename FROM _schema_migrations")
                applied = {row["filename"] for row in cur.fetchall()}
            if all(path.name in applied for path in files):
                self._conn.commit()
                return True
            self._conn.rollback()
            return False
        except UndefinedTable:
            self._conn.rollback()
            return False

    def apply_schema(self) -> None:
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if self._schema_is_current(files):
            return
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_ADVISORY_LOCK,))
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS _schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("SELECT filename FROM _schema_migrations")
            applied = {row["filename"] for row in cur.fetchall()}
            for path in files:
                if path.name in applied:
                    continue
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO _schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def start_run(self, run_id: str, query: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (run_id, query, status)
                VALUES (%s, %s, 'running')
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, query),
            )
        self._conn.commit()

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
            for alias in entity.aliases:
                normalized = normalize_surface(alias)
                if normalized:
                    cur.execute(
                        """
                        INSERT INTO entity_aliases (
                            entity_id, alias, normalized_alias, language,
                            alias_type, confidence, source, verified
                        ) VALUES (%s, %s, %s, %s, 'synonym', %s, %s, %s)
                        ON CONFLICT (entity_id, normalized_alias) DO NOTHING
                        """,
                        (
                            entity.id, alias, normalized, entity.language,
                            entity.confidence, entity.source, entity.source == "manual",
                        ),
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
        stored = value.value if isinstance(value, Enum) else value
        clauses = [
            "attributes @> %s::jsonb",
            "attributes ->> %s = %s",
        ]
        params: list[Any] = [Jsonb({key: stored}), key, str(stored)]
        columns = {
            "id": "id",
            "name": "name",
            "description": "description",
            "source": "source",
            "entity_type": "entity_type",
        }
        column = columns.get(key)
        if column:
            clauses.append(f"{column} = %s")
            params.append(str(stored))
        if key == "confidence":
            clauses.append("confidence = %s")
            params.append(stored)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (id) *
                FROM entities
                WHERE {' OR '.join(clauses)}
                ORDER BY id
                """,
                params,
            )
            rows = cur.fetchall()
        return [record_to_entity(row) for row in rows]

    def search(self, query: str) -> list[Entity]:
        pattern = f"%{query}%"
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.*
                FROM entities e
                WHERE e.name ILIKE %s
                   OR e.description ILIKE %s
                   OR COALESCE(e.attributes->>'canonical_name', '') ILIKE %s
                   OR COALESCE(e.attributes->>'aliases', '') ILIKE %s
                   OR EXISTS (
                        SELECT 1 FROM entity_aliases a
                        WHERE a.entity_id = e.id AND a.alias ILIKE %s
                   )
                """,
                (pattern, pattern, pattern, pattern, pattern),
            )
            rows = cur.fetchall()
        return [record_to_entity(row) for row in rows]

    def add_alias(
        self,
        entity_id: str,
        alias: str,
        *,
        language: str = "",
        alias_type: str = "synonym",
        confidence: float = 1.0,
        source: str = "manual",
        verified: bool = False,
    ) -> None:
        normalized = normalize_surface(alias)
        if not normalized:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO entity_aliases (
                        entity_id, alias, normalized_alias, language, alias_type,
                        confidence, source, verified
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (entity_id, normalized_alias) DO UPDATE SET
                        confidence = GREATEST(entity_aliases.confidence, EXCLUDED.confidence),
                        verified = entity_aliases.verified OR EXCLUDED.verified
                    """,
                    (
                        entity_id, alias, normalized, language, alias_type,
                        confidence, source, verified,
                    ),
                )
                cur.execute(
                    """
                    UPDATE entities
                    SET attributes = jsonb_set(
                        attributes,
                        '{aliases}',
                        (
                            SELECT to_jsonb(ARRAY(
                                SELECT DISTINCT a.alias
                                FROM entity_aliases a
                                WHERE a.entity_id = %s
                                ORDER BY a.alias
                            ))
                        ),
                        true
                    ), updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (entity_id, entity_id),
                )
                row = cur.fetchone()
            if row and self.record_outbox:
                self._insert_outbox("upsert_entity", record_to_entity(row).to_dict())
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def aliases_for(self, entity_id: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = %s ORDER BY alias",
                (entity_id,),
            )
            return [str(row["alias"]) for row in cur.fetchall()]

    def search_entity_candidates(
        self,
        query: str,
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized = normalize_surface(query)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT e.*, NULL::text AS matched_alias,
                           GREATEST(
                               similarity(LOWER(e.name), %s),
                               similarity(LOWER(COALESCE(e.attributes->>'canonical_name', '')), %s)
                           ) AS fuzzy_score,
                           CASE
                             WHEN LOWER(e.name) = %s THEN 1.0
                             WHEN LOWER(COALESCE(e.attributes->>'canonical_name', '')) = %s THEN 1.0
                             ELSE 0.0
                           END AS exact_score
                    FROM entities e
                    WHERE (%s::text[] IS NULL OR e.entity_type = ANY(%s::text[]))
                    UNION ALL
                    SELECT e.*, a.alias AS matched_alias,
                           similarity(a.normalized_alias, %s) AS fuzzy_score,
                           CASE WHEN a.normalized_alias = %s THEN 1.0 ELSE 0.0 END AS exact_score
                    FROM entity_aliases a
                    JOIN entities e ON e.id = a.entity_id
                    WHERE (%s::text[] IS NULL OR e.entity_type = ANY(%s::text[]))
                )
                SELECT * FROM (
                    SELECT DISTINCT ON (id) *
                    FROM candidates
                    WHERE exact_score > 0 OR fuzzy_score >= 0.15
                    ORDER BY id, exact_score DESC, fuzzy_score DESC
                ) ranked
                ORDER BY exact_score DESC, fuzzy_score DESC
                LIMIT %s
                """,
                (
                    normalized, normalized, normalized, normalized,
                    entity_types, entity_types,
                    normalized, normalized, entity_types, entity_types, limit,
                ),
            )
            rows = cur.fetchall()
        return [
            {
                "entity": record_to_entity(row),
                "matched_alias": row.get("matched_alias"),
                "exact_score": float(row.get("exact_score") or 0),
                "fuzzy_score": float(row.get("fuzzy_score") or 0),
            }
            for row in rows
        ]

    def record_mention(self, payload: dict[str, Any]) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_mentions (
                    run_id, document_id, surface_form, normalized_surface, context,
                    entity_type_hint, resolved_entity_id, status, confidence,
                    source_url, evidence, candidates, linker_version
                ) VALUES (
                    %(run_id)s, %(document_id)s, %(surface_form)s, %(normalized_surface)s,
                    %(context)s, %(entity_type_hint)s, %(resolved_entity_id)s, %(status)s,
                    %(confidence)s, %(source_url)s, %(evidence)s, %(candidates)s,
                    %(linker_version)s
                ) RETURNING id
                """,
                {
                    **payload,
                    "evidence": Json(payload.get("evidence") or {}),
                    "candidates": Json(payload.get("candidates") or []),
                },
            )
            mention_id = int(cur.fetchone()["id"])
        self._conn.commit()
        return mention_id

    def enqueue_linking_review(
        self, mention_id: int, candidates: list[dict[str, Any]]
    ) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO linking_review_queue (mention_id, candidates)
                VALUES (%s, %s) RETURNING id
                """,
                (mention_id, Json(candidates)),
            )
            review_id = int(cur.fetchone()["id"])
        self._conn.commit()
        return review_id

    def list_linking_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.*, m.surface_form, m.context, m.source_url, m.entity_type_hint
                FROM linking_review_queue q
                JOIN entity_mentions m ON m.id = q.mention_id
                WHERE q.status = %s ORDER BY q.id
                """,
                (status,),
            )
            return list(cur.fetchall())

    def resolve_linking_review(
        self, review_id: int, status: str, entity_id: Optional[str], notes: str = ""
    ) -> None:
        surface_form = ""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE linking_review_queue
                    SET status = %s, resolved_entity_id = %s, notes = %s,
                        resolved_at = NOW()
                    WHERE id = %s
                    RETURNING mention_id
                    """,
                    (status, entity_id, notes, review_id),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE entity_mentions
                        SET status = %s, resolved_entity_id = %s
                        WHERE id = %s
                        """,
                        ("LINKED" if entity_id else status.upper(), entity_id, row["mention_id"]),
                    )
                    cur.execute(
                        "SELECT surface_form FROM entity_mentions WHERE id = %s",
                        (row["mention_id"],),
                    )
                    mention = cur.fetchone()
                    surface_form = str(mention["surface_form"]) if mention else ""
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if entity_id and surface_form:
            self.add_alias(
                entity_id,
                surface_form,
                source="linking_review",
                confidence=1.0,
                verified=True,
            )

    def add_assertion(self, payload: dict[str, Any]) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_assertions (
                    run_id, source_mention_id, target_mention_id, subject_entity_id,
                    object_entity_id, predicate, value, confidence, source_url,
                    evidence_text, status
                ) VALUES (
                    %(run_id)s, %(source_mention_id)s, %(target_mention_id)s,
                    %(subject_entity_id)s, %(object_entity_id)s, %(predicate)s,
                    %(value)s, %(confidence)s, %(source_url)s, %(evidence_text)s, %(status)s
                ) RETURNING id
                """,
                {**payload, "value": Json(payload.get("value"))},
            )
            assertion_id = int(cur.fetchone()["id"])
        self._conn.commit()
        return assertion_id

    def save_embedding(
        self, entity_id: str, model: str, embedding: list[float], content_hash: str
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_embeddings (
                    entity_id, model, dimensions, embedding, content_hash
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET
                    model = EXCLUDED.model,
                    dimensions = EXCLUDED.dimensions,
                    embedding = EXCLUDED.embedding,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = NOW()
                """,
                (entity_id, model, len(embedding), Json(embedding), content_hash),
            )
            cur.execute(
                """
                UPDATE entities
                SET attributes = jsonb_set(attributes, '{embedding}', %s, true),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (Jsonb(embedding), entity_id),
            )
            row = cur.fetchone()
        if row and self.record_outbox:
            self._insert_outbox("upsert_entity", record_to_entity(row).to_dict())
        self._conn.commit()

    def search_by_embedding(
        self,
        embedding: list[float],
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
        model: str = "",
    ) -> list[dict[str, Any]]:
        """Cosine-rank stored embeddings when Neo4j vector search is unavailable."""
        import os

        model_name = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.*, emb.embedding AS stored_embedding
                FROM entity_embeddings emb
                JOIN entities e ON e.id = emb.entity_id
                WHERE emb.model = %s
                  AND (%s::text[] IS NULL OR e.entity_type = ANY(%s::text[]))
                LIMIT 5000
                """,
                (model_name, entity_types, entity_types),
            )
            rows = cur.fetchall()
        scored = []
        for row in rows:
            vector = row.pop("stored_embedding") or []
            score = cosine_similarity(embedding, list(vector))
            if score <= 0:
                continue
            scored.append({
                "entity": record_to_entity(row),
                "vector_score": score,
            })
        scored.sort(key=lambda item: item["vector_score"], reverse=True)
        return scored[:limit]

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
