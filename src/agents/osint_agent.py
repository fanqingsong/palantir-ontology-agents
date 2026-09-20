"""OSINT Collector Agent - Open-source intelligence collection and entity extraction.

LIVE: Uses Tavily web search (or demo fallback) to collect open-source intelligence,
extracts typed entities against config/ontology_schema.yaml, and updates the ontology store.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.ontology.schema import Relationship, RelationshipType, entity_from_dict
from src.ontology.schema_def import OntologySchema, load_ontology_schema
from src.ontology.store import OntologyStore
from src.tools.web_search import SearchResult, search_web
from src.entity_linking.extractor import MentionExtractor
from src.entity_linking.models import LinkStatus
from src.entity_linking.normalizer import normalize_surface
from src.entity_linking.service import EntityLinkingService

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_NON_ID = re.compile(r"[^a-z0-9]+")


@dataclass
class OSINTResult:
    """Result from OSINT collection run."""
    query: str = ""
    search_results: list[dict[str, Any]] = field(default_factory=list)
    extracted_entities: list[dict[str, Any]] = field(default_factory=list)
    new_relationships: list[dict[str, Any]] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    sources_consulted: int = 0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "search_results": self.search_results,
            "extracted_entities": self.extracted_entities,
            "new_relationships": self.new_relationships,
            "key_findings": self.key_findings,
            "sources_consulted": self.sources_consulted,
            "timestamp": self.timestamp,
        }


class OSINTAgent:
    """Open-source intelligence collection agent.

    Searches the web for relevant intelligence, extracts entities that match the
    local ontology schema, and updates the ontology store with new findings.
    """

    def __init__(
        self,
        ontology_store: Optional[OntologyStore] = None,
        llm: Any = None,
        schema: Optional[OntologySchema] = None,
    ):
        self.store = ontology_store
        self.llm = llm
        self.schema = schema or load_ontology_schema()
        self.run_id = ""

    def run(self, query: str, max_results: int = 5, run_id: str = "") -> OSINTResult:
        """Execute OSINT collection for a given query."""
        self.run_id = run_id
        result = OSINTResult(query=query)

        search_queries = self._generate_search_queries(query)
        all_search_results: list[SearchResult] = []
        for sq in search_queries:
            all_search_results.extend(search_web(sq, max_results=max_results))

        seen_urls: set[str] = set()
        unique_results: list[SearchResult] = []
        for sr in all_search_results:
            if sr.url not in seen_urls:
                seen_urls.add(sr.url)
                unique_results.append(sr)

        result.search_results = [
            {"title": sr.title, "url": sr.url, "content": sr.content[:500],
             "score": sr.score, "published_date": sr.published_date}
            for sr in unique_results
        ]
        result.sources_consulted = len(unique_results)

        extracted = self._extract_entities(unique_results)
        relationships = self._infer_relationships(unique_results)
        if self.llm and unique_results:
            llm_entities, llm_relationships = self._extract_with_schema(unique_results)
            extracted = _merge_entities(extracted, llm_entities)
            relationships = _merge_relationships(relationships, llm_relationships)

        result.extracted_entities = extracted
        result.new_relationships = relationships
        result.key_findings = self._extract_key_findings(unique_results)

        if self.store:
            self._update_ontology(result)
        return result

    def _generate_search_queries(self, query: str) -> list[str]:
        """Generate multiple search queries from the input query."""
        if self.llm:
            from src.agents.llm_support import bullet_lines, invoke_llm, load_prompt
            raw = invoke_llm(
                self.llm,
                load_prompt("osint"),
                "Generate 3 concise web search queries for this intelligence requirement. "
                "Return one query per line and nothing else.\n\n"
                f"Query: {query}",
            )
            generated = [line[:200] for line in bullet_lines(raw, limit=3)]
            if generated:
                return generated

        queries = [query]
        lower = query.lower()
        if "taiwan" in lower or "strait" in lower:
            queries.append("taiwan strait military exercises 2026")
            queries.append("semiconductor supply chain disruption")
        if "supply chain" in lower:
            queries.append("TSMC production delays shipping")
        if "military" in lower or "defense" in lower:
            queries.append("us military western pacific deployment")
        return queries[:3]

    def _gazetteer(self) -> list[tuple[str, str, str]]:
        """(pattern, entity_id, entity_type) from schema-valid store instances."""
        from src.ontology.schema_def import instance_gazetteer

        return instance_gazetteer(self.store, self.schema)

    def _extract_entities(self, results: list[SearchResult]) -> list[dict[str, Any]]:
        """Match search text against ontology instances whose types are in the schema."""
        found_entities: dict[str, dict[str, Any]] = {}
        gazetteer = self._gazetteer()
        for sr in results:
            text = (sr.title + " " + sr.content).lower()
            matched_in_result: set[str] = set()
            for pattern, eid, etype in gazetteer:
                if pattern not in text or eid in matched_in_result:
                    continue
                matched_in_result.add(eid)
                if eid not in found_entities:
                    name = eid
                    if self.store:
                        entity = self.store.get_entity(eid)
                        if entity:
                            name = entity.name
                    found_entities[eid] = {
                        "id": eid,
                        "name": name,
                        "type": etype,
                        "mentioned_in": [sr.url],
                        "mention_count": 1,
                        "attributes": {},
                    }
                else:
                    found_entities[eid]["mention_count"] += 1
                    if sr.url not in found_entities[eid]["mentioned_in"]:
                        found_entities[eid]["mentioned_in"].append(sr.url)
        return list(found_entities.values())

    def _extract_with_schema(
        self, results: list[SearchResult]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Extract mentions/assertions, then link before any canonical write."""
        blob = "\n".join(
            f"- {sr.title} ({sr.url}): {sr.content[:800]}" for sr in results[:8]
        )
        extractor = MentionExtractor(self.llm, self.schema.prompt_block())
        mentions, assertions = extractor.extract(blob, document_id="osint_batch")
        if not mentions:
            return [], []
        for mention in mentions:
            if mention.expected_types:
                mention.attributes = self.schema.coerce_attributes(
                    mention.expected_types[0], mention.attributes
                )

        linker = EntityLinkingService(self.store, llm=self.llm) if self.store else None
        if not linker:
            return [], []
        decisions = linker.link(mentions, mode="write", persist=False)
        for decision in decisions:
            if decision.status == LinkStatus.NEW:
                linker.create_canonical_entity(decision)
            linker.persist_decision(decision, self.run_id)
        decision_by_mention = {d.mention.mention_id: d for d in decisions}

        entities: list[dict[str, Any]] = []
        for decision in decisions:
            if decision.status not in (LinkStatus.LINKED, LinkStatus.NEW) or not decision.entity_id:
                continue
            entity = self.store.get_entity(decision.entity_id)
            if not entity:
                continue
            if normalize_surface(decision.mention.text) != normalize_surface(entity.name):
                self.store.add_alias(
                    entity.id,
                    decision.mention.text,
                    source="osint_agent",
                    confidence=decision.confidence,
                )
                linker.refresh_embedding(entity.id)
            entities.append({
                "id": entity.id,
                "name": entity.name,
                "type": entity.entity_type.value,
                "mentioned_in": [decision.mention.source_url] if decision.mention.source_url else [],
                "mention_count": 1,
                "confidence": decision.confidence,
                "attributes": dict(decision.mention.attributes),
                "link_status": decision.status.value,
            })

        relationships: list[dict[str, Any]] = []
        for assertion in assertions:
            source = decision_by_mention.get(assertion.source_mention_id)
            target = (
                decision_by_mention.get(assertion.target_mention_id)
                if assertion.target_mention_id else None
            )
            rel_type = self.schema.coerce_relationship_type(assertion.predicate)
            source_id = source.entity_id if source else None
            target_id = target.entity_id if target else None
            assertion_status = (
                "confirmed" if source_id and target_id and rel_type else "pending"
            )
            self.store.add_assertion({
                "run_id": self.run_id or None,
                "source_mention_id": source.persisted_mention_id if source else None,
                "target_mention_id": target.persisted_mention_id if target else None,
                "subject_entity_id": source_id,
                "object_entity_id": target_id,
                "predicate": rel_type or assertion.predicate,
                "value": assertion.value,
                "confidence": assertion.confidence,
                "source_url": assertion.source_url,
                "evidence_text": assertion.evidence_text,
                "status": assertion_status,
            })
            if assertion_status == "confirmed" and source_id != target_id:
                relationships.append({
                    "source": source_id,
                    "target": target_id,
                    "type": rel_type,
                    "source_url": assertion.source_url,
                    "confidence": assertion.confidence,
                })
        return entities, relationships

    def _entity_catalog(self) -> str:
        if not self.store:
            return ""
        lines = []
        for entity in self.store.all_entities():
            etype = entity.entity_type.value
            if self.schema.is_entity_type(etype):
                lines.append(f"- {entity.id} ({etype}): {entity.name}")
        return "\n".join(lines[:80])

    def _normalize_extracted_entity(self, item: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        etype = str(item.get("type") or item.get("entity_type") or "").strip().lower()
        if not self.schema.is_entity_type(etype):
            return None
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        attributes = self.schema.coerce_attributes(etype, item.get("attributes") or {})
        eid = self._resolve_entity_id(
            str(item.get("canonical_id") or item.get("id") or ""),
            name,
            [],
        ) or _slug_id(name)
        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        return {
            "id": eid,
            "name": name,
            "type": etype,
            "mentioned_in": [item["source_url"]] if item.get("source_url") else [],
            "mention_count": 1,
            "confidence": max(0.0, min(1.0, confidence)),
            "attributes": attributes,
            "description": str(item.get("description") or ""),
        }

    def _normalize_extracted_relationship(
        self, item: dict[str, Any], entities: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        rel_type = self.schema.coerce_relationship_type(str(item.get("type") or ""))
        if not rel_type:
            return None
        source_id = self._resolve_entity_id(str(item.get("source") or ""), "", entities)
        target_id = self._resolve_entity_id(str(item.get("target") or ""), "", entities)
        if not source_id or not target_id or source_id == target_id:
            return None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return {
            "source": source_id,
            "target": target_id,
            "type": rel_type,
            "source_url": str(item.get("source_url") or ""),
            "confidence": max(0.0, min(1.0, confidence)),
        }

    def _resolve_entity_id(
        self, token: str, name: str, extracted: list[dict[str, Any]]
    ) -> Optional[str]:
        candidates = [value.strip() for value in (token, name) if value and value.strip()]
        for candidate in candidates:
            if self.store:
                if self.store.get_entity(candidate):
                    return candidate
                by_name = self.store.get_entity_by_name(candidate)
                if by_name and self.schema.is_entity_type(by_name.entity_type.value):
                    return by_name.id
            lower = candidate.lower()
            for entity in extracted:
                if entity["id"] == candidate or entity["name"].lower() == lower:
                    return entity["id"]
            if self.store:
                for pattern, eid, _etype in self._gazetteer():
                    if pattern == lower:
                        return eid
        return None

    def _extract_key_findings(self, results: list[SearchResult]) -> list[str]:
        """Extract key findings from search results."""
        if self.llm and results:
            from src.agents.llm_support import bullet_lines, invoke_llm, load_prompt
            blob = "\n".join(
                f"- {sr.title}: {sr.content[:300]}" for sr in results[:8]
            )
            raw = invoke_llm(
                self.llm,
                load_prompt("osint"),
                "Extract up to 6 key intelligence findings from these search results. "
                "Return one finding per line.\n\n"
                f"{blob}",
            )
            findings = bullet_lines(raw, limit=8)
            if findings:
                return findings

        findings: list[str] = []
        for sr in results:
            if sr.score >= 0.85:
                content = sr.content.strip()
                first_sentence = content.split(". ")[0] + "."
                if len(first_sentence) > 20:
                    findings.append(first_sentence)
        return findings[:8]

    def _infer_relationships(self, results: list[SearchResult]) -> list[dict[str, Any]]:
        """Co-occurrence remains document evidence; it is not a canonical graph edge."""
        return []

    def _update_ontology(self, result: OSINTResult) -> None:
        """Write schema-valid entities and relationships into the store."""
        for item in result.extracted_entities:
            self._upsert_extracted_entity(item)

        for rel in result.new_relationships:
            source_id = rel.get("source")
            target_id = rel.get("target")
            rel_type = self.schema.coerce_relationship_type(str(rel.get("type") or ""))
            if not source_id or not target_id or not rel_type:
                continue
            if not self.store.get_entity(source_id) or not self.store.get_entity(target_id):
                continue
            rel_id = _relationship_id(source_id, target_id, rel_type)
            if self.store.get_relationship(rel_id):
                continue
            self.store.add_relationship(Relationship(
                id=rel_id,
                source_id=source_id,
                target_id=target_id,
                relationship_type=RelationshipType(rel_type),
                confidence=float(rel.get("confidence", 0.5)),
                description="OSINT extraction",
                attributes={"source_url": rel.get("source_url", "")},
            ))

    def _upsert_extracted_entity(self, item: dict[str, Any]) -> None:
        etype = str(item.get("type") or "")
        if not self.schema.is_entity_type(etype):
            return
        eid = str(item.get("id") or "")
        if eid and self.store.get_entity(eid):
            return
        name = str(item.get("name") or "").strip()
        if not name:
            return
        by_name = self.store.get_entity_by_name(name)
        if by_name:
            item["id"] = by_name.id
            return
        attributes = self.schema.coerce_attributes(etype, item.get("attributes") or {})
        payload = {
            "id": eid or _slug_id(name),
            "name": name,
            "entity_type": etype,
            "description": str(item.get("description") or name),
            "source": "osint_agent",
            "confidence": float(item.get("confidence", 0.7)),
            "tags": ["osint_extracted"],
        }
        payload.update(attributes)
        entity = entity_from_dict(payload)
        self.store.add_entity(entity)
        item["id"] = entity.id


def _slug_id(name: str) -> str:
    slug = _NON_ID.sub("_", name.lower()).strip("_")[:48]
    return slug or str(uuid.uuid4())[:8]


def _relationship_id(source_id: str, target_id: str, rel_type: str) -> str:
    if rel_type == "RELATED_TO":
        return f"osint_{source_id}_{target_id}"
    return f"osint_{source_id}_{target_id}_{rel_type}"


def _merge_entities(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {item["id"]: dict(item) for item in base}
    for item in extra:
        existing = merged.get(item["id"])
        if not existing:
            merged[item["id"]] = dict(item)
            continue
        existing["mention_count"] = max(
            int(existing.get("mention_count") or 1),
            int(item.get("mention_count") or 1),
        )
        for url in item.get("mentioned_in") or []:
            if url not in existing.setdefault("mentioned_in", []):
                existing["mentioned_in"].append(url)
        attrs = dict(existing.get("attributes") or {})
        attrs.update(item.get("attributes") or {})
        existing["attributes"] = attrs
        if item.get("name"):
            existing["name"] = item["name"]
    return list(merged.values())


def _merge_relationships(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for item in base + extra:
        key = (item["source"], item["target"], item["type"])
        alt = (item["target"], item["source"], item["type"])
        if key in seen or alt in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    cleaned = _JSON_FENCE.sub("", (text or "").strip()).strip()
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
