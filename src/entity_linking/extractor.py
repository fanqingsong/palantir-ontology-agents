"""Structured mention and assertion extraction."""

from __future__ import annotations

import json
import re
from typing import Any

from src.entity_linking.models import Assertion, Mention
from src.entity_linking.normalizer import normalize_surface

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class MentionExtractor:
    def __init__(self, llm: Any, schema_prompt: str) -> None:
        self.llm = llm
        self.schema_prompt = schema_prompt

    def extract(
        self, text: str, *, source_url: str = "", document_id: str = ""
    ) -> tuple[list[Mention], list[Assertion]]:
        if not self.llm:
            return [], []
        from src.agents.llm_support import invoke_llm

        raw = invoke_llm(
            self.llm,
            "You extract mentions and assertions without assigning ontology IDs.\n"
            + self.schema_prompt,
            "Return JSON only: "
            '{"mentions":[{"mention_id":"m1","text":"","expected_types":[],"context":"",'
            '"semantic_role":"","confidence":0.0,"attributes":{},"source_url":""}],'
            '"assertions":[{"source_mention_id":"m1","target_mention_id":"m2",'
            '"predicate":"","value":null,"confidence":0.0,"evidence_text":"",'
            '"source_url":""}]}.\n'
            f"Text:\n{text}",
        )
        try:
            payload = json.loads(_FENCE.sub("", raw.strip()))
        except (json.JSONDecodeError, TypeError):
            return [], []
        raw_mentions = payload.get("mentions")
        if raw_mentions is None:
            raw_mentions = [
                {
                    "mention_id": str(item.get("canonical_id") or item.get("id") or f"m{index + 1}"),
                    "text": item.get("name"),
                    "expected_types": [item.get("type") or item.get("entity_type")]
                    if item.get("type") or item.get("entity_type") else [],
                    "confidence": item.get("confidence", 0.7),
                    "attributes": item.get("attributes") or {},
                }
                for index, item in enumerate(payload.get("entities") or [])
                if isinstance(item, dict)
            ]
            known_ids = {str(item.get("mention_id")) for item in raw_mentions}
            for relationship in payload.get("relationships") or []:
                for endpoint in ("source", "target"):
                    token = str(relationship.get(endpoint) or "")
                    if token and token not in known_ids:
                        raw_mentions.append({
                            "mention_id": token,
                            "text": token,
                            "expected_types": [],
                            "confidence": relationship.get("confidence", 0.7),
                        })
                        known_ids.add(token)
        mentions = []
        for index, item in enumerate(raw_mentions or []):
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            mention = Mention(
                mention_id=str(item.get("mention_id") or f"m{index + 1}"),
                text=str(item["text"]).strip(),
                expected_types=[str(v).lower() for v in item.get("expected_types") or []],
                semantic_role=str(item.get("semantic_role") or ""),
                context=str(item.get("context") or text[:500]),
                source_url=str(item.get("source_url") or source_url),
                document_id=str(item.get("document_id") or document_id),
                start=_optional_int(item.get("start")),
                end=_optional_int(item.get("end")),
                confidence=_confidence(item.get("confidence"), 0.7),
                attributes=dict(item.get("attributes") or {}),
            )
            mention.normalized_text = normalize_surface(mention.text)
            mentions.append(mention)
        assertions = []
        raw_assertions = payload.get("assertions")
        if raw_assertions is None:
            raw_assertions = [
                {
                    "source_mention_id": item.get("source"),
                    "target_mention_id": item.get("target"),
                    "predicate": item.get("type"),
                    "confidence": item.get("confidence", 0.7),
                    "evidence_text": item.get("description", ""),
                }
                for item in payload.get("relationships") or []
                if isinstance(item, dict)
            ]
        for item in raw_assertions or []:
            if not isinstance(item, dict) or not item.get("source_mention_id"):
                continue
            assertions.append(Assertion(
                source_mention_id=str(item["source_mention_id"]),
                target_mention_id=(
                    str(item["target_mention_id"]) if item.get("target_mention_id") else None
                ),
                predicate=str(item.get("predicate") or "RELATED_TO").upper(),
                value=item.get("value"),
                confidence=_confidence(item.get("confidence"), 0.7),
                source_url=str(item.get("source_url") or source_url),
                evidence_text=str(item.get("evidence_text") or ""),
            ))
        return mentions, assertions


def _confidence(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
