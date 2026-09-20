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

    def start_run(self, run_id: str, query: str) -> None:
        method = getattr(self._backend, "start_run", None)
        if callable(method):
            method(run_id, query)

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

    def add_alias(self, entity_id: str, alias: str, **metadata: Any) -> None:
        method = getattr(self._backend, "add_alias", None)
        if callable(method):
            method(entity_id, alias, **metadata)

    def aliases_for(self, entity_id: str) -> list[str]:
        method = getattr(self._backend, "aliases_for", None)
        if callable(method):
            return list(method(entity_id))
        entity = self.get_entity(entity_id)
        return list(entity.aliases) if entity else []

    def search_entity_candidates(
        self,
        query: str,
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        method = getattr(self._backend, "search_entity_candidates", None)
        if callable(method):
            return list(method(query, entity_types, limit))
        return [
            {"entity": entity, "exact_score": 0.0, "fuzzy_score": 0.5}
            for entity in self.search(query)[:limit]
        ]

    def record_mention(self, payload: dict[str, Any]) -> Optional[int]:
        method = getattr(self._backend, "record_mention", None)
        return int(method(payload)) if callable(method) else None

    def enqueue_linking_review(
        self, mention_id: int, candidates: list[dict[str, Any]]
    ) -> Optional[int]:
        method = getattr(self._backend, "enqueue_linking_review", None)
        return int(method(mention_id, candidates)) if callable(method) else None

    def list_linking_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        method = getattr(self._backend, "list_linking_reviews", None)
        return list(method(status)) if callable(method) else []

    def resolve_linking_review(
        self, review_id: int, status: str, entity_id: Optional[str], notes: str = ""
    ) -> None:
        method = getattr(self._backend, "resolve_linking_review", None)
        if callable(method):
            method(review_id, status, entity_id, notes)

    def add_assertion(self, payload: dict[str, Any]) -> Optional[int]:
        method = getattr(self._backend, "add_assertion", None)
        return int(method(payload)) if callable(method) else None

    def save_embedding(
        self, entity_id: str, model: str, embedding: list[float], content_hash: str
    ) -> None:
        method = getattr(self._backend, "save_embedding", None)
        if callable(method):
            method(entity_id, model, embedding, content_hash)

    def graph_search_backend(self) -> Any:
        method = getattr(self._backend, "graph_search_backend", None)
        if callable(method):
            return method()
        if self._backend.__class__.__name__ == "Neo4jBackend":
            return self._backend
        raise RuntimeError("Agentic graph search requires the Neo4j or dual backend")

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
