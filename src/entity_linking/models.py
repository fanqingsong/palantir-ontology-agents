"""Typed contracts shared by OSINT ingestion and graph-query entity linking."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class LinkStatus(str, Enum):
    LINKED = "LINKED"
    NEW = "NEW"
    AMBIGUOUS = "AMBIGUOUS"
    NIL = "NIL"
    DEFERRED = "DEFERRED"


@dataclass
class Mention:
    text: str
    mention_id: str = field(default_factory=lambda: f"mention_{uuid.uuid4().hex[:12]}")
    normalized_text: str = ""
    expected_types: list[str] = field(default_factory=list)
    semantic_role: str = ""
    context: str = ""
    source_url: str = ""
    document_id: str = ""
    start: int | None = None
    end: int | None = None
    confidence: float = 0.7
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntityCandidate:
    entity_id: str
    name: str
    entity_type: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    graph_context: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LinkDecision:
    mention: Mention
    status: LinkStatus
    entity_id: str | None = None
    confidence: float = 0.0
    candidates: list[EntityCandidate] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    persisted_mention_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass
class Assertion:
    predicate: str
    source_mention_id: str
    target_mention_id: str | None = None
    value: Any = None
    confidence: float = 0.7
    source_url: str = ""
    evidence_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
