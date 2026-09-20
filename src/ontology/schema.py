"""Ontology type definitions: typed entities and relationships for intelligence analysis."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class EntityType(str, Enum):
    ORGANIZATION = "organization"
    PERSON = "person"
    LOCATION = "location"
    EVENT = "event"
    ASSET = "asset"
    THREAT = "threat"


class RelationshipType(str, Enum):
    OPERATES_IN = "OPERATES_IN"
    SUPPLIES = "SUPPLIES"
    THREATENS = "THREATENS"
    RELATED_TO = "RELATED_TO"
    HEADQUARTERED_IN = "HEADQUARTERED_IN"
    COMMANDS = "COMMANDS"
    DEPLOYED_AT = "DEPLOYED_AT"
    MANUFACTURES = "MANUFACTURES"
    SHIPS_THROUGH = "SHIPS_THROUGH"
    DEPENDS_ON = "DEPENDS_ON"
    ALLIES_WITH = "ALLIES_WITH"
    COMPETES_WITH = "COMPETES_WITH"
    MONITORS = "MONITORS"
    OWNS = "OWNS"
    EMPLOYS = "EMPLOYS"
    PARTICIPATES_IN = "PARTICIPATES_IN"
    TRANSITS = "TRANSITS"
    SUPPLIES_TO = "SUPPLIES_TO"
    LOCATED_IN = "LOCATED_IN"


@dataclass
class Entity:
    """Base entity in the ontology graph."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    canonical_name: str = ""
    aliases: list[str] = field(default_factory=list)
    language: str = ""
    external_ids: dict[str, str] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
    entity_type: EntityType = EntityType.ORGANIZATION
    description: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source: str = "manual"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["canonical_name"]:
            d["canonical_name"] = self.name
        d["entity_type"] = self.entity_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        data = data.copy()
        data["entity_type"] = EntityType(data["entity_type"])
        return cls(**data)


@dataclass
class Organization(Entity):
    """An organization: company, government body, military unit, NGO."""

    entity_type: EntityType = field(default=EntityType.ORGANIZATION)
    org_type: str = ""  # corporation, government, military, ngo
    country: str = ""
    sector: str = ""
    revenue_usd: Optional[float] = None


@dataclass
class Person(Entity):
    """A person of interest in the intelligence graph."""

    entity_type: EntityType = field(default=EntityType.PERSON)
    role: str = ""
    nationality: str = ""
    affiliation: str = ""


@dataclass
class Location(Entity):
    """A geographic location: port, city, region, military base."""

    entity_type: EntityType = field(default=EntityType.LOCATION)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_type: str = ""  # port, city, region, base, strait
    country: str = ""


@dataclass
class Event(Entity):
    """A discrete event: military exercise, trade disruption, diplomatic incident."""

    entity_type: EntityType = field(default=EntityType.EVENT)
    event_type: str = ""  # military_exercise, trade_disruption, diplomatic, natural_disaster
    start_date: str = ""
    end_date: str = ""
    severity: str = "medium"  # low, medium, high, critical


@dataclass
class Asset(Entity):
    """A strategic asset: ship, aircraft, facility, technology."""

    entity_type: EntityType = field(default=EntityType.ASSET)
    asset_type: str = ""  # vessel, aircraft, facility, technology, semiconductor
    operator: str = ""
    status: str = "active"  # active, deployed, under_construction, decommissioned


@dataclass
class Threat(Entity):
    """A threat entity: military buildup, cyber threat, economic coercion."""

    entity_type: EntityType = field(default=EntityType.THREAT)
    threat_type: str = ""  # military, cyber, economic, hybrid
    severity: str = "medium"
    likelihood: float = 0.5
    impact_description: str = ""


@dataclass
class Relationship:
    """A typed edge between two entities in the ontology graph."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    source_id: str = ""
    target_id: str = ""
    relationship_type: RelationshipType = RelationshipType.RELATED_TO
    weight: float = 1.0
    attributes: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    confidence: float = 1.0
    source_label: str = ""  # populated at query time
    target_label: str = ""  # populated at query time
    bidirectional: bool = False
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["relationship_type"] = self.relationship_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Relationship:
        data = data.copy()
        data["relationship_type"] = RelationshipType(data["relationship_type"])
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


ENTITY_CLASS_BY_TYPE: dict[EntityType, type[Entity]] = {
    EntityType.ORGANIZATION: Organization,
    EntityType.PERSON: Person,
    EntityType.LOCATION: Location,
    EntityType.EVENT: Event,
    EntityType.ASSET: Asset,
    EntityType.THREAT: Threat,
}

TYPE_SPECIFIC_FIELDS: dict[EntityType, tuple[str, ...]] = {
    EntityType.ORGANIZATION: ("org_type", "country", "sector", "revenue_usd"),
    EntityType.PERSON: ("role", "nationality", "affiliation"),
    EntityType.LOCATION: ("latitude", "longitude", "location_type", "country"),
    EntityType.EVENT: ("event_type", "start_date", "end_date", "severity"),
    EntityType.ASSET: ("asset_type", "operator", "status"),
    EntityType.THREAT: ("threat_type", "severity", "likelihood", "impact_description"),
}


def entity_from_dict(data: dict[str, Any]) -> Entity:
    """Reconstruct a typed entity from a serialized dict."""
    payload = data.copy()
    raw_type = payload.get("entity_type", EntityType.ORGANIZATION)
    entity_type = raw_type if isinstance(raw_type, EntityType) else EntityType(raw_type)
    payload["entity_type"] = entity_type
    cls = ENTITY_CLASS_BY_TYPE.get(entity_type, Entity)
    allowed = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in payload.items() if k in allowed})
