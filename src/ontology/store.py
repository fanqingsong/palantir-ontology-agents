"""Ontology store facade. Agents depend on this API, not a specific backend."""

from __future__ import annotations

from typing import Any, Optional

from src.ontology.memory import MemoryBackend
from src.ontology.schema import Entity, EntityType, Relationship, RelationshipType


class OntologyStore:
    """Typed ontology graph. Defaults to an in-memory backend."""

    def __init__(self, backend: Any = None) -> None:
        self._backend = backend or MemoryBackend()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OntologyStore:
        store = cls()
        store.load_dict(data)
        return store

    def load_dict(self, data: dict[str, Any]) -> None:
        self._backend.load_dict(data)

    def add_entity(self, entity: Entity) -> str:
        return self._backend.add_entity(entity)

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self._backend.get_entity(entity_id)

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        return self._backend.get_entity_by_name(name)

    def remove_entity(self, entity_id: str) -> bool:
        return self._backend.remove_entity(entity_id)

    def add_relationship(self, relationship: Relationship) -> str:
        return self._backend.add_relationship(relationship)

    def get_relationship(self, rel_id: str) -> Optional[Relationship]:
        return self._backend.get_relationship(rel_id)

    def query_by_type(self, entity_type: EntityType) -> list[Entity]:
        return self._backend.query_by_type(entity_type)

    def query_by_attribute(self, key: str, value: Any) -> list[Entity]:
        return self._backend.query_by_attribute(key, value)

    def search(self, query: str) -> list[Entity]:
        return self._backend.search(query)

    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        direction: str = "both",
    ) -> list[tuple[Entity, Relationship]]:
        return self._backend.get_neighbors(entity_id, relationship_type, direction)

    def traverse(
        self,
        entity_id: str,
        hops: int = 2,
        relationship_type: Optional[RelationshipType] = None,
    ) -> dict[str, Any]:
        return self._backend.traverse(entity_id, hops=hops, relationship_type=relationship_type)

    def find_path(self, source_id: str, target_id: str, max_hops: int = 5) -> Optional[list[str]]:
        return self._backend.find_path(source_id, target_id, max_hops=max_hops)

    def get_dependency_chains(
        self,
        entity_id: str,
        rel_types: Optional[list[RelationshipType]] = None,
        max_depth: int = 5,
    ) -> list[list[str]]:
        return self._backend.get_dependency_chains(
            entity_id, rel_types=rel_types, max_depth=max_depth
        )

    def calculate_exposure_score(
        self, entity_id: str, threat_ids: Optional[list[str]] = None
    ) -> float:
        return self._backend.calculate_exposure_score(entity_id, threat_ids=threat_ids)

    def to_dict(self) -> dict[str, Any]:
        return self._backend.to_dict()

    def snapshot_stats(self) -> dict[str, Any]:
        payload = self._backend.to_dict()
        return payload.get("stats", {})

    def drain_outbox(self) -> int:
        flush = getattr(self._backend, "flush_outbox", None)
        if callable(flush):
            return int(flush() or 0)
        return 0

    @property
    def entity_count(self) -> int:
        return self._backend.entity_count

    @property
    def relationship_count(self) -> int:
        return self._backend.relationship_count

    def all_entities(self) -> list[Entity]:
        return self._backend.all_entities()

    def all_relationships(self) -> list[Relationship]:
        return self._backend.all_relationships()
