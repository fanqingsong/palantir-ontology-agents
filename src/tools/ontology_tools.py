"""Ontology query and traversal tools for agent use.

LIVE: Provides typed access to the ontology store for querying entities,
traversing relationships, and analyzing graph structure.
"""

from __future__ import annotations

from typing import Any, Optional

from src.ontology.schema import EntityType, RelationshipType
from src.ontology.store import OntologyStore


def _entity_names(
    store: OntologyStore,
    entity_ids: list[str],
    *,
    missing: Optional[str] = None,
) -> dict[str, str]:
    """Resolve each id once. Path naming used to query the same entity repeatedly."""
    names: dict[str, str] = {}
    for entity_id in entity_ids:
        if entity_id in names:
            continue
        entity = store.get_entity(entity_id)
        if entity:
            names[entity_id] = entity.name
        else:
            names[entity_id] = entity_id if missing is None else missing
    return names


def query_entities(store: OntologyStore, entity_type: Optional[str] = None,
                   search_term: Optional[str] = None) -> list[dict[str, Any]]:
    """Query entities from the ontology store.

    Args:
        store: The ontology store to query.
        entity_type: Optional entity type filter (organization, person, location, etc.).
        search_term: Optional text search across names and descriptions.

    Returns:
        List of entity dictionaries.
    """
    if entity_type:
        et = EntityType(entity_type.lower())
        entities = store.query_by_type(et)
    elif search_term:
        entities = store.search(search_term)
    else:
        entities = store.all_entities()

    return [e.to_dict() for e in entities]


def traverse_entity(store: OntologyStore, entity_id: str, hops: int = 2,
                    relationship_type: Optional[str] = None) -> dict[str, Any]:
    """Traverse the ontology graph from a starting entity.

    Args:
        store: The ontology store.
        entity_id: Starting entity ID.
        hops: Number of hops to traverse (default 2).
        relationship_type: Optional filter for relationship type.

    Returns:
        Traversal result with entities, relationships, and paths.
    """
    rel_type = RelationshipType(relationship_type) if relationship_type else None
    result = store.traverse(entity_id, hops=hops, relationship_type=rel_type)

    flat_ids = [eid for path in result["paths"] for eid in path]
    names = _entity_names(store, flat_ids, missing="unknown")
    enriched_paths = [
        [{"id": eid, "name": names.get(eid, "unknown")} for eid in path]
        for path in result["paths"]
    ]

    result["named_paths"] = enriched_paths
    result["entities"] = list(result["entities"])
    result["relationships"] = list(result["relationships"])
    return result


def find_dependency_chains(
    store: OntologyStore,
    entity_id: str,
    max_depth: int = 5,
    rel_types: Optional[list[RelationshipType]] = None,
) -> list[list[dict[str, str]]]:
    """Find supply chain / dependency chains from an entity."""
    chains = store.get_dependency_chains(
        entity_id, rel_types=rel_types, max_depth=max_depth
    )
    names = _entity_names(store, [eid for chain in chains for eid in chain])
    return [
        [{"id": eid, "name": names.get(eid, eid)} for eid in chain]
        for chain in chains
    ]


def get_exposure_report(
    store: OntologyStore,
    entity_ids: Optional[list[str]] = None,
    entity_types: Optional[list[EntityType]] = None,
    threat_entity_type: EntityType = EntityType.THREAT,
) -> list[dict[str, Any]]:
    """Calculate exposure scores for entities."""
    if entity_ids is None:
        types = entity_types or [EntityType.ORGANIZATION]
        entities = []
        for entity_type in types:
            entities.extend(store.query_by_type(entity_type))
    else:
        entities = []
        for eid in entity_ids:
            entity = store.get_entity(eid)
            if entity:
                entities.append(entity)

    threat_ids = [e.id for e in store.query_by_type(threat_entity_type)]
    scores = store.exposure_scores([entity.id for entity in entities], threat_ids)
    report = []
    for entity in entities:
        report.append({
            "entity_id": entity.id,
            "name": entity.name,
            "type": entity.entity_type.value,
            "exposure_score": scores.get(entity.id, 0.0),
        })

    report.sort(key=lambda x: x["exposure_score"], reverse=True)
    return report


def find_shortest_path(store: OntologyStore, source_id: str, target_id: str) -> Optional[list[dict[str, str]]]:
    """Find shortest path between two entities.

    Args:
        store: The ontology store.
        source_id: Source entity ID.
        target_id: Target entity ID.

    Returns:
        List of {id, name} dicts representing the path, or None.
    """
    path = store.find_path(source_id, target_id)
    if path is None:
        return None
    names = _entity_names(store, path)
    return [{"id": eid, "name": names.get(eid, eid)} for eid in path]
