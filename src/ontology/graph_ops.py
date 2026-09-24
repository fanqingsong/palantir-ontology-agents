"""Shared graph algorithms used by all ontology backends."""

from __future__ import annotations

from collections import deque
from typing import Any, Optional, Protocol

from src.ontology.schema import Entity, EntityType, Relationship, RelationshipType

# find_path's default horizon. A path one hop past this scores 0, same as no path.
_EXPOSURE_MAX_HOPS = 5


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
    frontier: deque[tuple[str, list[str], int]] = deque([(entity_id, [entity_id], 0)])

    while frontier:
        current_id, current_path, depth = frontier.popleft()
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
    found = shortest_paths_from(view, source_id, [target_id], max_hops=max_hops)
    return found.get(target_id)


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


def shortest_paths_from(
    view: GraphView,
    source_id: str,
    target_ids: list[str],
    max_hops: int = 5,
) -> dict[str, list[str]]:
    """One BFS from ``source_id`` to many targets.

    The first time a target is reached is its shortest path, using the same
    neighbor order as a single-target search.
    """
    remaining = {target_id for target_id in target_ids if target_id}
    found: dict[str, list[str]] = {}
    if source_id in remaining:
        found[source_id] = [source_id]
        remaining.remove(source_id)
    if not remaining:
        return found

    visited = {source_id}
    queue: deque[tuple[str, list[str]]] = deque([(source_id, [source_id])])
    while queue and remaining:
        current, path = queue.popleft()
        if len(path) > max_hops + 1:
            break
        for neighbor, _rel in view.get_neighbors(current):
            neighbor_id = neighbor.id
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            new_path = path + [neighbor_id]
            if neighbor_id in remaining:
                found[neighbor_id] = new_path
                remaining.remove(neighbor_id)
            queue.append((neighbor_id, new_path))
    return found


def exposure_from_hops(hops: Optional[int]) -> float:
    if hops is None:
        return 0.0
    return round(max(0.0, 1.0 - (hops - 1) * 0.2), 2)


def exposure_scores(
    view: GraphView,
    entity_ids: list[str],
    threat_ids: list[str],
) -> dict[str, float]:
    """Score many entities with one multi-source BFS out of the threats.

    Distance is undirected, matching ``find_path``. Hops past the scoring
    horizon are 0, the same as an unreachable threat.
    """
    if not threat_ids:
        return {entity_id: 0.0 for entity_id in entity_ids}

    distances: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque()
    for threat_id in dict.fromkeys(threat_ids):
        if threat_id not in distances:
            distances[threat_id] = 0
            queue.append((threat_id, 0))

    pending = {entity_id for entity_id in entity_ids if entity_id not in distances}
    while queue and pending:
        current, hops = queue.popleft()
        if hops >= _EXPOSURE_MAX_HOPS:
            continue
        for neighbor, _rel in view.get_neighbors(current):
            neighbor_id = neighbor.id
            if neighbor_id in distances:
                continue
            distances[neighbor_id] = hops + 1
            pending.discard(neighbor_id)
            queue.append((neighbor_id, hops + 1))

    return {
        entity_id: exposure_from_hops(distances.get(entity_id))
        for entity_id in entity_ids
    }


def calculate_exposure_score(
    view: GraphView,
    entity_id: str,
    threat_ids: Optional[list[str]] = None,
) -> float:
    if threat_ids is None:
        threat_ids = [e.id for e in view.query_by_type(EntityType.THREAT)]
    if not threat_ids:
        return 0.0
    if entity_id in threat_ids:
        return exposure_from_hops(0)

    # Expand from the entity and stop at the nearest threat. That is cheaper
    # than walking every threat, and BFS makes the first hit the closest.
    threat_set = set(threat_ids)
    visited = {entity_id}
    queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
    while queue:
        current, hops = queue.popleft()
        if hops >= _EXPOSURE_MAX_HOPS:
            continue
        for neighbor, _rel in view.get_neighbors(current):
            neighbor_id = neighbor.id
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            next_hops = hops + 1
            if neighbor_id in threat_set:
                return exposure_from_hops(next_hops)
            queue.append((neighbor_id, next_hops))
    return 0.0


def degree_by_entity(view: GraphView) -> dict[str, int]:
    """Count each relationship once at every distinct endpoint."""
    counts: dict[str, int] = {}
    for relationship in view.all_relationships():
        for endpoint in {relationship.source_id, relationship.target_id}:
            if endpoint:
                counts[endpoint] = counts.get(endpoint, 0) + 1
    return counts


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
