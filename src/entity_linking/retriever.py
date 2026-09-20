"""Hybrid exact, fuzzy, full-text, and vector candidate retrieval."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from src.entity_linking.embeddings import EmbeddingProvider
from src.entity_linking.models import EntityCandidate, Mention
from src.entity_linking.normalizer import normalize_surface
from src.ontology.store import OntologyStore


class HybridCandidateRetriever:
    def __init__(
        self,
        store: OntologyStore,
        embedding_provider: EmbeddingProvider | None = None,
        limit: int = 20,
    ) -> None:
        self.store = store
        self.embeddings = embedding_provider or EmbeddingProvider()
        self.limit = limit

    def retrieve(self, mention: Mention) -> tuple[list[EntityCandidate], list[str]]:
        by_id: dict[str, EntityCandidate] = {}
        degraded: list[str] = []
        query_embedding: list[float] = []
        if self.embeddings.available:
            try:
                query_embedding = self.embeddings.embed_query(
                    f"{mention.text}\n{mention.context}\n{' '.join(mention.expected_types)}"
                )
            except Exception as exc:
                degraded.append(f"embedding unavailable: {exc}")
        else:
            degraded.append("embedding unavailable: OPENAI_API_KEY is not configured")

        direct = self.store.get_entity(mention.text)
        if direct and (
            not mention.expected_types or direct.entity_type.value in mention.expected_types
        ):
            self._merge(by_id, {
                "entity": direct,
                "exact_score": 1.0,
                "fuzzy_score": 1.0,
            })
        for item in self.store.search_entity_candidates(
            mention.text, mention.expected_types or None, self.limit
        ):
            self._merge(by_id, item)

        try:
            graph = self.store.graph_search_backend()
            for item in graph.search_entity_candidates(
                mention.text,
                mention.expected_types or None,
                self.limit,
                query_embedding,
            ):
                self._merge(by_id, item)
        except Exception as exc:
            degraded.append(f"neo4j candidate retrieval unavailable: {exc}")

        normalized = normalize_surface(mention.text)
        for candidate in by_id.values():
            forms = [candidate.name, *candidate.aliases]
            fuzzy = max(
                (
                    SequenceMatcher(None, normalized, normalize_surface(form)).ratio()
                    for form in forms if form
                ),
                default=0.0,
            )
            candidate.scores["fuzzy"] = max(candidate.scores.get("fuzzy", 0.0), fuzzy)
            candidate.scores["type"] = (
                1.0 if not mention.expected_types or
                candidate.entity_type in mention.expected_types else 0.0
            )
        return list(by_id.values()), degraded

    def _merge(self, by_id: dict[str, EntityCandidate], item: dict[str, Any]) -> None:
        entity = item["entity"]
        candidate = by_id.setdefault(
            entity.id,
            EntityCandidate(
                entity_id=entity.id,
                name=entity.name,
                entity_type=entity.entity_type.value,
                description=entity.description,
                aliases=list(getattr(entity, "aliases", []) or self.store.aliases_for(entity.id)),
            ),
        )
        aliases = list(candidate.aliases)
        if item.get("matched_alias") and item["matched_alias"] not in aliases:
            aliases.append(str(item["matched_alias"]))
        candidate.aliases = aliases
        for source, target in (
            ("exact_score", "exact"),
            ("fuzzy_score", "fuzzy"),
            ("fulltext_score", "fulltext"),
            ("vector_score", "vector"),
        ):
            candidate.scores[target] = max(
                candidate.scores.get(target, 0.0), float(item.get(source) or 0.0)
            )
