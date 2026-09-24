"""In-memory ontology backend."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
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
from src.entity_linking.embeddings import cosine_similarity
from src.entity_linking.normalizer import normalize_surface


class MemoryBackend:
    """In-memory graph store for ontology objects."""

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._name_index: dict[str, str] = {}
        self._relationships: dict[str, Relationship] = {}
        self._outgoing: dict[str, list[str]] = defaultdict(list)
        self._incoming: dict[str, list[str]] = defaultdict(list)
        self._type_index: dict[EntityType, set[str]] = defaultdict(set)
        self._aliases: dict[str, list[str]] = defaultdict(list)
        self._mentions: list[dict[str, Any]] = []
        self._mentions_by_id: dict[int, dict[str, Any]] = {}
        self._reviews: list[dict[str, Any]] = []
        self._assertions: list[dict[str, Any]] = []

    def add_entity(self, entity: Entity) -> str:
        previous = self._entities.get(entity.id)
        old_name = previous.name.lower() if previous else None
        self._entities[entity.id] = entity
        self._type_index[entity.entity_type].add(entity.id)
        new_name = entity.name.lower()
        if old_name and old_name != new_name:
            self._reindex_name(old_name)
        if new_name and self._name_index.get(new_name) != entity.id:
            if new_name not in self._name_index:
                self._name_index[new_name] = entity.id
            else:
                self._reindex_name(new_name)
        return entity.id

    def _reindex_name(self, name_key: str) -> None:
        """Point a name at the earliest entity, matching a full scan."""
        if not name_key:
            return
        for other in self._entities.values():
            if other.name.lower() == name_key:
                self._name_index[name_key] = other.id
                return
        self._name_index.pop(name_key, None)

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self._entities.get(entity_id)

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        entity_id = self._name_index.get(name.lower())
        if not entity_id:
            return None
        entity = self._entities.get(entity_id)
        if entity and entity.name.lower() == name.lower():
            return entity
        self._reindex_name(name.lower())
        entity_id = self._name_index.get(name.lower())
        return self._entities.get(entity_id) if entity_id else None

    def remove_entity(self, entity_id: str) -> bool:
        if entity_id not in self._entities:
            return False
        entity = self._entities.pop(entity_id)
        self._type_index[entity.entity_type].discard(entity_id)
        self._reindex_name(entity.name.lower())
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
            haystacks = [
                entity.name,
                entity.canonical_name,
                entity.description,
                *entity.aliases,
                *self._aliases.get(entity.id, []),
            ]
            if any(query_lower in (value or "").lower() for value in haystacks):
                results.append(entity)
        return results

    def add_alias(self, entity_id: str, alias: str, **_: Any) -> None:
        entity = self.get_entity(entity_id)
        if not entity:
            return
        normalized = normalize_surface(alias)
        existing = {normalize_surface(value) for value in self._aliases[entity_id]}
        if normalized and normalized not in existing:
            self._aliases[entity_id].append(alias)
        entity.aliases = list(self._aliases[entity_id])

    def aliases_for(self, entity_id: str) -> list[str]:
        entity = self.get_entity(entity_id)
        return list(self._aliases.get(entity_id) or (entity.aliases if entity else []))

    def search_entity_candidates(
        self,
        query: str,
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized = normalize_surface(query)
        candidates = []
        for entity in self._entities.values():
            if entity_types and entity.entity_type.value not in entity_types:
                continue
            names = [
                entity.name,
                entity.canonical_name,
                *entity.aliases,
                *self._aliases.get(entity.id, []),
            ]
            scored = [
                (
                    value,
                    1.0 if normalize_surface(value) == normalized else
                    SequenceMatcher(None, normalize_surface(value), normalized).ratio(),
                )
                for value in names if value
            ]
            if not scored:
                continue
            matched, score = max(scored, key=lambda item: item[1])
            if score >= 0.15:
                candidates.append({
                    "entity": entity,
                    "matched_alias": matched if matched != entity.name else None,
                    "exact_score": 1.0 if score == 1.0 else 0.0,
                    "fuzzy_score": score,
                })
        candidates.sort(
            key=lambda item: (item["exact_score"], item["fuzzy_score"]), reverse=True
        )
        return candidates[:limit]

    def search_by_embedding(
        self,
        embedding: list[float],
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        scored = []
        for entity in self._entities.values():
            if entity_types and entity.entity_type.value not in entity_types:
                continue
            score = cosine_similarity(embedding, entity.embedding)
            if score <= 0:
                continue
            scored.append({
                "entity": entity,
                "vector_score": score,
            })
        scored.sort(key=lambda item: item["vector_score"], reverse=True)
        return scored[:limit]

    def record_mention(self, payload: dict[str, Any]) -> int:
        mention_id = len(self._mentions) + 1
        row = {"id": mention_id, **payload}
        self._mentions.append(row)
        self._mentions_by_id[mention_id] = row
        return mention_id

    def enqueue_linking_review(
        self, mention_id: int, candidates: list[dict[str, Any]]
    ) -> int:
        review_id = len(self._reviews) + 1
        self._reviews.append({
            "id": review_id,
            "mention_id": mention_id,
            "status": "pending",
            "candidates": candidates,
        })
        return review_id

    def list_linking_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = []
        for item in self._reviews:
            if item["status"] != status:
                continue
            mention = self._mentions_by_id.get(item["mention_id"], {})
            rows.append({**item, **{
                key: mention.get(key)
                for key in ("surface_form", "context", "source_url", "entity_type_hint")
            }})
        return rows

    def resolve_linking_review(
        self, review_id: int, status: str, entity_id: Optional[str], notes: str = ""
    ) -> None:
        for review in self._reviews:
            if review["id"] == review_id:
                review.update(status=status, resolved_entity_id=entity_id, notes=notes)
                if entity_id:
                    mention = self._mentions_by_id.get(review["mention_id"])
                    if mention:
                        self.add_alias(entity_id, mention["surface_form"])
                break

    def add_assertion(self, payload: dict[str, Any]) -> int:
        assertion_id = len(self._assertions) + 1
        self._assertions.append({"id": assertion_id, **payload})
        return assertion_id

    def save_embedding(
        self, entity_id: str, model: str, embedding: list[float], content_hash: str
    ) -> None:
        entity = self.get_entity(entity_id)
        if entity:
            entity.embedding = list(embedding)

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

    def stats(self) -> dict[str, Any]:
        type_distribution: dict[str, int] = {}
        for entity in self._entities.values():
            key = entity.entity_type.value
            type_distribution[key] = type_distribution.get(key, 0) + 1
        return {
            "entity_count": len(self._entities),
            "relationship_count": len(self._relationships),
            "type_distribution": type_distribution,
        }

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
