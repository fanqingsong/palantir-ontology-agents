"""Postgres + Neo4j dual backend: PG is authoritative, Neo4j is a projection via outbox."""

from __future__ import annotations

import os
from typing import Any, Optional

from src.ontology.neo4j_backend import Neo4jBackend
from src.ontology.outbox_projector import OutboxProjector
from src.ontology.postgres_backend import PostgresBackend
from src.ontology.schema import Entity, EntityType, Relationship, RelationshipType


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

    def flush_outbox(self) -> int:
        return self._projector.drain_pending(worker_id="dual-backend")

    def _maybe_flush(self) -> None:
        if _sync_flush_on_write():
            self.flush_outbox()

    def _graph(self):
        try:
            self.flush_outbox()
            return self.neo4j
        except Exception:
            return self.postgres

    def add_entity(self, entity: Entity) -> str:
        entity_id = self.postgres.add_entity(entity)
        self._maybe_flush()
        return entity_id

    def add_relationship(self, relationship: Relationship) -> str:
        rel_id = self.postgres.add_relationship(relationship)
        self._maybe_flush()
        return rel_id

    def remove_entity(self, entity_id: str) -> bool:
        removed = self.postgres.remove_entity(entity_id)
        if removed:
            self._maybe_flush()
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
