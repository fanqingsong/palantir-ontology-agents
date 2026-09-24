"""Shared entity-linking pipeline for ingestion and query-time resolution."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from src.entity_linking.coherence import apply_graph_coherence
from src.entity_linking.embeddings import (
    EmbeddingProvider,
    content_hash,
    entity_embedding_text,
)
from src.entity_linking.models import EntityCandidate, LinkDecision, LinkStatus, Mention
from src.entity_linking.normalizer import normalize_surface
from src.entity_linking.reranker import rerank
from src.entity_linking.retriever import HybridCandidateRetriever
from src.ontology.schema import Entity, EntityType
from src.ontology.schema_def import load_ontology_schema
from src.ontology.store import OntologyStore

logger = logging.getLogger(__name__)


class EntityLinkingService:
    VERSION = "v1"

    def __init__(self, store: OntologyStore, llm: Any = None) -> None:
        self.store = store
        self.llm = llm
        self.config = load_ontology_schema().linking_config
        self.retriever = HybridCandidateRetriever(
            store,
            limit=int(os.environ.get(
                "ENTITY_LINK_CANDIDATE_LIMIT",
                str(self.config.get("candidate_limit", 20)),
            )),
        )
        self.embeddings = self.retriever.embeddings

    def link(
        self,
        mentions: list[Mention],
        *,
        mode: str = "query",
        persist: bool = False,
        run_id: str = "",
    ) -> list[LinkDecision]:
        deduplicated = self._deduplicate_mentions(mentions)
        candidate_sets: list[list[EntityCandidate]] = []
        degraded_by_mention: list[list[str]] = []
        for mention in deduplicated:
            mention.normalized_text = mention.normalized_text or normalize_surface(mention.text)
            candidates, degraded = self.retriever.retrieve(mention)
            candidate_sets.append(rerank(candidates))
            degraded_by_mention.append(degraded)
        apply_graph_coherence(self.store, candidate_sets)
        candidate_sets = [rerank(candidates) for candidates in candidate_sets]

        decisions = [
            self._decide(mention, candidates, degraded, mode)
            for mention, candidates, degraded in zip(
                deduplicated, candidate_sets, degraded_by_mention
            )
        ]
        if persist:
            for decision in decisions:
                self._persist(decision, run_id)
        return decisions

    def persist_decision(self, decision: LinkDecision, run_id: str = "") -> None:
        self._persist(decision, run_id)

    def create_canonical_entity(self, decision: LinkDecision, *, embed: bool = True) -> Entity:
        if decision.status != LinkStatus.NEW:
            raise ValueError("Only NEW decisions may create canonical entities")
        mention = decision.mention
        entity_type = (
            EntityType(mention.expected_types[0])
            if mention.expected_types else EntityType.EVENT
        )
        entity = Entity(
            id=f"ent_{uuid.uuid4().hex}",
            name=mention.text,
            canonical_name=mention.text,
            aliases=[],
            entity_type=entity_type,
            description=mention.context[:500],
            source="osint_agent",
            confidence=mention.confidence,
            attributes=dict(mention.attributes),
            tags=["osint_extracted"],
        )
        self.store.add_entity(entity)
        decision.entity_id = entity.id
        if embed:
            self.refresh_embedding(entity.id)
        return entity

    def refresh_embedding(self, entity_id: str) -> bool:
        return self.refresh_embeddings([entity_id]) == 1

    def refresh_embeddings(self, entity_ids: list[str]) -> int:
        if not self.embeddings.available:
            return 0
        entities = []
        texts = []
        for entity_id in dict.fromkeys(entity_ids):
            entity = self.store.get_entity(entity_id)
            if not entity:
                continue
            entities.append(entity)
            texts.append(entity_embedding_text(entity))
        if not texts:
            return 0
        try:
            vectors = self.embeddings.embed_documents(texts)
        except Exception:
            logger.warning("Embedding refresh failed for %s entities", len(texts), exc_info=True)
            return 0
        saved = 0
        for entity, text, vector in zip(entities, texts, vectors):
            if not vector:
                continue
            entity.embedding = vector
            self.store.save_embedding(
                entity.id, self.embeddings.model, vector, content_hash(text)
            )
            saved += 1
        return saved

    def _decide(
        self,
        mention: Mention,
        candidates: list[EntityCandidate],
        degraded: list[str],
        mode: str,
    ) -> LinkDecision:
        if not candidates:
            status = (
                LinkStatus.NEW
                if mode == "write" and mention.confidence >=
                float(os.environ.get(
                    "ENTITY_LINK_NEW_MIN_CONFIDENCE",
                    str(self.config.get("new_entity_min_confidence", 0.90)),
                ))
                else LinkStatus.NIL
            )
            return LinkDecision(
                mention=mention,
                status=status,
                confidence=mention.confidence if status == LinkStatus.NEW else 0.0,
                reasons=["no candidate found", *degraded],
            )

        top = candidates[0]
        second_score = candidates[1].score if len(candidates) > 1 else 0.0
        threshold = float(os.environ.get(
            "ENTITY_LINK_WRITE_THRESHOLD" if mode == "write"
            else "ENTITY_LINK_QUERY_THRESHOLD",
            str(self.config.get(
                "write_auto_link_threshold" if mode == "write"
                else "query_auto_link_threshold",
                0.90 if mode == "write" else 0.72,
            )),
        ))
        gap = float(os.environ.get(
            "ENTITY_LINK_REVIEW_GAP", str(self.config.get("review_gap", 0.08))
        ))
        if top.score >= threshold and top.score - second_score >= gap:
            return LinkDecision(
                mention=mention,
                status=LinkStatus.LINKED,
                entity_id=top.entity_id,
                confidence=top.score,
                candidates=candidates[:10],
                reasons=["hybrid candidate score passed threshold", *degraded],
            )

        selected = self._agent_adjudicate(mention, candidates[:5])
        if selected:
            candidate = next((c for c in candidates if c.entity_id == selected), None)
            if candidate:
                return LinkDecision(
                    mention=mention,
                    status=LinkStatus.LINKED,
                    entity_id=candidate.entity_id,
                    confidence=max(candidate.score, 0.75),
                    candidates=candidates[:10],
                    reasons=["agent resolved candidate using mention context", *degraded],
                )
        return LinkDecision(
            mention=mention,
            status=LinkStatus.AMBIGUOUS,
            confidence=top.score,
            candidates=candidates[:10],
            reasons=["candidate score or margin requires review", *degraded],
        )

    def _agent_adjudicate(
        self, mention: Mention, candidates: list[EntityCandidate]
    ) -> str | None:
        if not self.llm or not candidates:
            return None
        from src.agents.llm_support import invoke_llm

        raw = invoke_llm(
            self.llm,
            "Resolve an entity mention only when the context clearly identifies one candidate. "
            'Return JSON only: {"entity_id": string|null, "reason": string}.',
            json.dumps({
                "mention": mention.to_dict(),
                "candidates": [candidate.to_dict() for candidate in candidates],
            }, ensure_ascii=False),
        )
        try:
            payload = json.loads(raw.strip().removeprefix("```json").removesuffix("```"))
        except (json.JSONDecodeError, TypeError):
            return None
        entity_id = payload.get("entity_id")
        return str(entity_id) if entity_id else None

    def _persist(self, decision: LinkDecision, run_id: str) -> None:
        mention = decision.mention
        mention_id = self.store.record_mention({
            "run_id": run_id or None,
            "document_id": mention.document_id,
            "surface_form": mention.text,
            "normalized_surface": mention.normalized_text,
            "context": mention.context,
            "entity_type_hint": mention.expected_types[0] if mention.expected_types else None,
            "resolved_entity_id": decision.entity_id,
            "status": decision.status.value,
            "confidence": decision.confidence,
            "source_url": mention.source_url,
            "evidence": {
                "semantic_role": mention.semantic_role,
                "start": mention.start,
                "end": mention.end,
            },
            "candidates": [candidate.to_dict() for candidate in decision.candidates],
            "linker_version": self.VERSION,
        })
        decision.persisted_mention_id = mention_id
        if mention_id and decision.status in (LinkStatus.AMBIGUOUS, LinkStatus.NIL):
            self.store.enqueue_linking_review(
                mention_id, [candidate.to_dict() for candidate in decision.candidates]
            )

    @staticmethod
    def _deduplicate_mentions(mentions: list[Mention]) -> list[Mention]:
        unique: dict[tuple[str, tuple[str, ...], str], Mention] = {}
        for mention in mentions:
            normalized = mention.normalized_text or normalize_surface(mention.text)
            key = (normalized, tuple(sorted(mention.expected_types)), mention.document_id)
            current = unique.get(key)
            if current is None or mention.confidence > current.confidence:
                mention.normalized_text = normalized
                unique[key] = mention
        return list(unique.values())
