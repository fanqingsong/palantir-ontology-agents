"""Shared graph algorithms used by all ontology backends."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from src.ontology.schema import Entity, EntityType, Relationship, RelationshipType


class GraphView(Protocol):
    def get_entity(self, entity_id: str) -> Optional[Entity]: ...
    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        direction: str = "both",
    ) -> list[tuple[Entity, Relationship]]: ...
    def query_by_type(self, entity_type: EntityType) -> list[Entity]: ...
    def all_entities(self) -> list[Entity]: ...
    def all_relationships(self) -> list[Relationship]: ...


def traverse(
    view: GraphView,
    entity_id: str,
    hops: int = 2,
    relationship_type: Optional[RelationshipType] = None,
) -> dict[str, Any]:
    visited_entities: set[str] = {entity_id}
    visited_rels: set[str] = set()
    paths: list[list[str]] = [[entity_id]]
    frontier = [(entity_id, [entity_id], 0)]

    while frontier:
        current_id, current_path, depth = frontier.pop(0)
        if depth >= hops:
            continue
        for neighbor, rel in view.get_neighbors(current_id, relationship_type):
            visited_rels.add(rel.id)
            new_path = current_path + [neighbor.id]
            if neighbor.id not in visited_entities:
                visited_entities.add(neighbor.id)
                paths.append(new_path)
                frontier.append((neighbor.id, new_path, depth + 1))

    return {
        "root": entity_id,
        "hops": hops,
        "entities": visited_entities,
        "relationships": visited_rels,
        "paths": paths,
        "entity_count": len(visited_entities),
        "relationship_count": len(visited_rels),
    }


def find_path(
    view: GraphView,
    source_id: str,
    target_id: str,
    max_hops: int = 5,
) -> Optional[list[str]]:
    if source_id == target_id:
        return [source_id]
    visited = {source_id}
    queue = [(source_id, [source_id])]
    while queue:
        current, path = queue.pop(0)
        if len(path) > max_hops + 1:
            break
        for neighbor, _ in view.get_neighbors(current):
            if neighbor.id == target_id:
                return path + [neighbor.id]
            if neighbor.id not in visited:
                visited.add(neighbor.id)
                queue.append((neighbor.id, path + [neighbor.id]))
    return None


def get_dependency_chains(
    view: GraphView,
    entity_id: str,
    rel_types: Optional[list[RelationshipType]] = None,
    max_depth: int = 5,
) -> list[list[str]]:
    if rel_types is None:
        rel_types = [
            RelationshipType.DEPENDS_ON,
            RelationshipType.SUPPLIES,
            RelationshipType.SUPPLIES_TO,
        ]
    chains: list[list[str]] = []
    stack = [(entity_id, [entity_id])]
    while stack:
        current, path = stack.pop()
        if len(path) > max_depth + 1:
            continue
        extended = False
        for neighbor, rel in view.get_neighbors(current, direction="outgoing"):
            if rel.relationship_type in rel_types and neighbor.id not in path:
                stack.append((neighbor.id, path + [neighbor.id]))
                extended = True
        if not extended and len(path) > 1:
            chains.append(path)
    return chains


def calculate_exposure_score(
    view: GraphView,
    entity_id: str,
    threat_ids: Optional[list[str]] = None,
) -> float:
    if threat_ids is None:
        threat_ids = [e.id for e in view.query_by_type(EntityType.THREAT)]
    if not threat_ids:
        return 0.0
    max_score = 0.0
    for tid in threat_ids:
        path = find_path(view, entity_id, tid)
        if path:
            hop_distance = len(path) - 1
            score = max(0, 1.0 - (hop_distance - 1) * 0.2)
            max_score = max(max_score, score)
    return round(max_score, 2)


def store_to_dict(view: GraphView) -> dict[str, Any]:
    entities = view.all_entities()
    relationships = view.all_relationships()
    type_distribution: dict[str, int] = {}
    for entity in entities:
        key = entity.entity_type.value
        type_distribution[key] = type_distribution.get(key, 0) + 1
    return {
        "entities": {e.id: e.to_dict() for e in entities},
        "relationships": {r.id: r.to_dict() for r in relationships},
        "stats": {
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "type_distribution": type_distribution,
        },
    }
