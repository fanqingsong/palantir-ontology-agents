"""Serialize ontology objects for Postgres and Neo4j."""

from __future__ import annotations

from typing import Any

from src.ontology.schema import (
    TYPE_SPECIFIC_FIELDS,
    Entity,
    EntityType,
    Relationship,
    entity_from_dict,
)


def entity_to_record(entity: Entity) -> dict[str, Any]:
    payload = entity.to_dict()
    attributes = dict(payload.get("attributes") or {})
    for key in ("canonical_name", "aliases", "language", "external_ids", "embedding"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            attributes[key] = value
    for key in TYPE_SPECIFIC_FIELDS.get(entity.entity_type, ()):
        if key in payload:
            attributes[key] = payload[key]
    return {
        "id": payload["id"],
        "entity_type": entity.entity_type.value,
        "name": payload.get("name", ""),
        "description": payload.get("description", ""),
        "tags": list(payload.get("tags") or []),
        "source": payload.get("source", "manual"),
        "confidence": payload.get("confidence", 1.0),
        "attributes": attributes,
        "created_at": payload.get("created_at", ""),
    }


def record_to_entity(record: dict[str, Any]) -> Entity:
    attributes = dict(record.get("attributes") or {})
    identity_fields = {
        key: attributes.pop(key)
        for key in ("canonical_name", "aliases", "language", "external_ids", "embedding")
        if key in attributes
    }
    payload = {
        "id": record["id"],
        "name": record.get("name", ""),
        "entity_type": record.get("entity_type", EntityType.ORGANIZATION),
        "description": record.get("description", ""),
        "tags": list(record.get("tags") or []),
        "source": record.get("source", "manual"),
        "confidence": record.get("confidence", 1.0),
        "created_at": record.get("created_at") or "",
        "attributes": {k: v for k, v in attributes.items() if k not in sum(TYPE_SPECIFIC_FIELDS.values(), ())},
    }
    payload.update(identity_fields)
    payload.update(attributes)
    if hasattr(payload.get("created_at"), "isoformat"):
        payload["created_at"] = payload["created_at"].isoformat()
    return entity_from_dict(payload)


def relationship_to_record(relationship: Relationship) -> dict[str, Any]:
    return relationship.to_dict()


def record_to_relationship(record: dict[str, Any]) -> Relationship:
    payload = dict(record)
    if "rel_type" in payload and "relationship_type" not in payload:
        payload["relationship_type"] = payload.pop("rel_type")
    if hasattr(payload.get("created_at"), "isoformat"):
        payload["created_at"] = payload["created_at"].isoformat()
    return Relationship.from_dict(payload)
