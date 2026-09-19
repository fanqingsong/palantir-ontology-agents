"""In-memory ontology backend."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from src.ontology.graph_ops import (
    calculate_exposure_score,
    find_path,
    get_dependency_chains,
    store_to_dict,
    traverse,
)
from src.ontology.schema import (
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
    entity_from_dict,
)


class MemoryBackend:
    """In-memory graph store for ontology objects."""

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._relationships: dict[str, Relationship] = {}
        self._outgoing: dict[str, list[str]] = defaultdict(list)
        self._incoming: dict[str, list[str]] = defaultdict(list)
        self._type_index: dict[EntityType, set[str]] = defaultdict(set)

    def add_entity(self, entity: Entity) -> str:
        self._entities[entity.id] = entity
        self._type_index[entity.entity_type].add(entity.id)
        return entity.id

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self._entities.get(entity_id)

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        name_lower = name.lower()
        for entity in self._entities.values():
            if entity.name.lower() == name_lower:
                return entity
        return None

    def remove_entity(self, entity_id: str) -> bool:
        if entity_id not in self._entities:
            return False
        entity = self._entities.pop(entity_id)
        self._type_index[entity.entity_type].discard(entity_id)
        rel_ids = set(self._outgoing.pop(entity_id, []) + self._incoming.pop(entity_id, []))
        for rid in rel_ids:
            self._relationships.pop(rid, None)
        return True

    def add_relationship(self, relationship: Relationship) -> str:
        self._relationships[relationship.id] = relationship
        self._outgoing[relationship.source_id].append(relationship.id)
        self._incoming[relationship.target_id].append(relationship.id)
        if relationship.bidirectional:
            self._outgoing[relationship.target_id].append(relationship.id)
            self._incoming[relationship.source_id].append(relationship.id)
        return relationship.id

    def get_relationship(self, rel_id: str) -> Optional[Relationship]:
        return self._relationships.get(rel_id)

    def query_by_type(self, entity_type: EntityType) -> list[Entity]:
        return [
            self._entities[eid]
            for eid in self._type_index.get(entity_type, set())
            if eid in self._entities
        ]

    def query_by_attribute(self, key: str, value: Any) -> list[Entity]:
        results = []
        for entity in self._entities.values():
            if entity.attributes.get(key) == value:
                results.append(entity)
            elif hasattr(entity, key) and getattr(entity, key) == value:
                results.append(entity)
        return results

    def search(self, query: str) -> list[Entity]:
        query_lower = query.lower()
        results = []
        for entity in self._entities.values():
            if query_lower in entity.name.lower() or query_lower in entity.description.lower():
                results.append(entity)
        return results

    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        direction: str = "both",
    ) -> list[tuple[Entity, Relationship]]:
        neighbors: list[tuple[Entity, Relationship]] = []
        rel_ids: set[str] = set()
        if direction in ("outgoing", "both"):
            rel_ids.update(self._outgoing.get(entity_id, []))
        if direction in ("incoming", "both"):
            rel_ids.update(self._incoming.get(entity_id, []))
        for rid in rel_ids:
            rel = self._relationships.get(rid)
            if rel is None:
                continue
            if relationship_type and rel.relationship_type != relationship_type:
                continue
            neighbor_id = rel.target_id if rel.source_id == entity_id else rel.source_id
            neighbor = self._entities.get(neighbor_id)
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
        for payload in data.get("entities", {}).values():
            self.add_entity(entity_from_dict(payload))
        for payload in data.get("relationships", {}).values():
            self.add_relationship(Relationship.from_dict(payload))

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    @property
    def relationship_count(self) -> int:
        return len(self._relationships)

    def all_entities(self) -> list[Entity]:
        return list(self._entities.values())

    def all_relationships(self) -> list[Relationship]:
        return list(self._relationships.values())
