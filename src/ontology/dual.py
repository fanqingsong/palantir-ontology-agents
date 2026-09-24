"""Postgres + Neo4j dual backend: PG is authoritative, Neo4j is a projection via outbox."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from src.ontology.neo4j_backend import Neo4jBackend
from src.ontology.outbox_projector import OutboxProjector
from src.ontology.postgres_backend import PostgresBackend
from src.ontology.schema import Entity, EntityType, Relationship, RelationshipType

logger = logging.getLogger(__name__)


def _sync_flush_on_write() -> bool:
    return os.environ.get("OUTBOX_SYNC_FLUSH", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class DualBackend:
    def __init__(self, postgres: PostgresBackend, neo4j: Neo4jBackend) -> None:
        postgres.record_outbox = True
        self.postgres = postgres
        self.neo4j = neo4j
        self._projector = OutboxProjector(postgres, neo4j)
        # Graph reads reuse the last successful drain until the next Postgres write.
        self._projection_ready = False
        self.projection_degraded = False
        self._read_backend: PostgresBackend | Neo4jBackend = neo4j

    def flush_outbox(self) -> int:
        processed = self._projector.drain_pending(worker_id="dual-backend")
        self._read_backend = self.neo4j
        self._projection_ready = True
        self.projection_degraded = False
        return processed

    def _maybe_flush(self) -> None:
        if _sync_flush_on_write():
            self.flush_outbox()

    def _note_write(self) -> None:
        """Postgres is ahead of Neo4j until the next explicit or lazy drain."""
        self._projection_ready = False
        self._maybe_flush()

    def _graph(self):
        if self._projection_ready:
            return self._read_backend
        try:
            self.flush_outbox()
        except Exception:
            logger.warning(
                "Neo4j projection drain failed; graph reads are using Postgres",
                exc_info=True,
            )
            self._read_backend = self.postgres
            self._projection_ready = True
            self.projection_degraded = True
        return self._read_backend

    def add_entity(self, entity: Entity) -> str:
        entity_id = self.postgres.add_entity(entity)
        self._note_write()
        return entity_id

    def start_run(self, run_id: str, query: str) -> None:
        self.postgres.start_run(run_id, query)

    def add_relationship(self, relationship: Relationship) -> str:
        rel_id = self.postgres.add_relationship(relationship)
        self._note_write()
        return rel_id

    def remove_entity(self, entity_id: str) -> bool:
        removed = self.postgres.remove_entity(entity_id)
        if removed:
            self._note_write()
        return removed

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self.postgres.get_entity(entity_id)

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        return self.postgres.get_entity_by_name(name)

    def get_relationship(self, rel_id: str) -> Optional[Relationship]:
        return self.postgres.get_relationship(rel_id)

    def query_by_type(self, entity_type: EntityType) -> list[Entity]:
        return self.postgres.query_by_type(entity_type)

    def query_by_attribute(self, key: str, value: Any) -> list[Entity]:
        return self.postgres.query_by_attribute(key, value)

    def search(self, query: str) -> list[Entity]:
        return self.postgres.search(query)

    def add_alias(self, entity_id: str, alias: str, **metadata: Any) -> None:
        self.postgres.add_alias(entity_id, alias, **metadata)
        self._note_write()

    def aliases_for(self, entity_id: str) -> list[str]:
        return self.postgres.aliases_for(entity_id)

    def search_entity_candidates(
        self,
        query: str,
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.postgres.search_entity_candidates(query, entity_types, limit)

    def record_mention(self, payload: dict[str, Any]) -> int:
        return self.postgres.record_mention(payload)

    def enqueue_linking_review(
        self, mention_id: int, candidates: list[dict[str, Any]]
    ) -> int:
        return self.postgres.enqueue_linking_review(mention_id, candidates)

    def list_linking_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        return self.postgres.list_linking_reviews(status)

    def resolve_linking_review(
        self, review_id: int, status: str, entity_id: Optional[str], notes: str = ""
    ) -> None:
        self.postgres.resolve_linking_review(review_id, status, entity_id, notes)

    def add_assertion(self, payload: dict[str, Any]) -> int:
        return self.postgres.add_assertion(payload)

    def save_embedding(
        self, entity_id: str, model: str, embedding: list[float], content_hash: str
    ) -> None:
        self.postgres.save_embedding(entity_id, model, embedding, content_hash)

    def graph_search_backend(self) -> Neo4jBackend:
        if not self._projection_ready:
            self.flush_outbox()
        self.neo4j.verify_connectivity()
        return self.neo4j

    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        direction: str = "both",
    ) -> list[tuple[Entity, Relationship]]:
        return self._graph().get_neighbors(entity_id, relationship_type, direction)

    def traverse(
        self,
        entity_id: str,
        hops: int = 2,
        relationship_type: Optional[RelationshipType] = None,
    ) -> dict[str, Any]:
        return self._graph().traverse(entity_id, hops=hops, relationship_type=relationship_type)

    def find_path(self, source_id: str, target_id: str, max_hops: int = 5) -> Optional[list[str]]:
        return self._graph().find_path(source_id, target_id, max_hops=max_hops)

    def get_dependency_chains(
        self,
        entity_id: str,
        rel_types: Optional[list[RelationshipType]] = None,
        max_depth: int = 5,
    ) -> list[list[str]]:
        return self._graph().get_dependency_chains(
            entity_id, rel_types=rel_types, max_depth=max_depth
        )

    def calculate_exposure_score(
        self, entity_id: str, threat_ids: Optional[list[str]] = None
    ) -> float:
        return self._graph().calculate_exposure_score(entity_id, threat_ids=threat_ids)

    def to_dict(self) -> dict[str, Any]:
        return self.postgres.to_dict()

    def load_dict(self, data: dict[str, Any]) -> None:
        self.postgres.load_dict(data)
        self.flush_outbox()

    @property
    def entity_count(self) -> int:
        return self.postgres.entity_count

    @property
    def relationship_count(self) -> int:
        return self.postgres.relationship_count

    def all_entities(self) -> list[Entity]:
        return self.postgres.all_entities()

    def all_relationships(self) -> list[Relationship]:
        return self.postgres.all_relationships()

    def close(self) -> None:
        self.postgres.close()
        self.neo4j.close()
