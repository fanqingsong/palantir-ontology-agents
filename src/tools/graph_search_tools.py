"""Bounded tools exposed to the agentic graph-search loop."""

from __future__ import annotations

from typing import Any

from src.ontology.schema_def import OntologySchema
from src.ontology.store import OntologyStore


def describe_graph_schema(schema: OntologySchema) -> dict[str, Any]:
    return {
        "node_label": "Entity",
        "entity_types": sorted(schema.entity_types),
        "relationship_types": sorted(schema.relationship_types),
        "rules": [
            "Use node property id for canonical entity identifiers",
            "Use parameters for all entity values",
            "Use bounded paths with at most 6 hops",
            "Every query must include LIMIT",
        ],
    }


def inspect_entity(store: OntologyStore, entity_id: str) -> dict[str, Any] | None:
    entity = store.get_entity(entity_id)
    if not entity:
        return None
    neighbors = []
    for neighbor, relationship in store.get_neighbors(entity_id):
        neighbors.append({
            "id": neighbor.id,
            "name": neighbor.name,
            "type": neighbor.entity_type.value,
            "relationship": relationship.relationship_type.value,
        })
    return {"entity": entity.to_dict(), "neighbors": neighbors[:30]}


def run_readonly_cypher(
    store: OntologyStore,
    cypher: str,
    parameters: dict[str, Any],
    *,
    max_rows: int = 200,
) -> dict[str, Any]:
    backend = store.graph_search_backend()
    return backend.execute_readonly(cypher, parameters, max_rows=max_rows)
