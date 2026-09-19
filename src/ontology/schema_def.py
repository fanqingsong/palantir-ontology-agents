"""Load config/ontology_schema.yaml as the runtime ontology contract."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from src.ontology.schema import EntityType, RelationshipType

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "ontology_schema.yaml"


@dataclass(frozen=True)
class AttributeSpec:
    name: str
    type: str
    optional: bool = True
    enum: tuple[str, ...] | None = None
    value_range: tuple[float, float] | None = None


@dataclass(frozen=True)
class EntityTypeSpec:
    name: str
    description: str
    attributes: tuple[AttributeSpec, ...]


@dataclass(frozen=True)
class OntologySchema:
    entity_types: dict[str, EntityTypeSpec]
    relationship_types: frozenset[str]
    base_attributes: tuple[AttributeSpec, ...]
    dependency_relationship_types: tuple[str, ...]
    threat_entity_type: str
    exposure_entity_types: tuple[str, ...]

    def is_entity_type(self, name: str) -> bool:
        return (name or "").strip().lower() in self.entity_types

    def is_relationship_type(self, name: str) -> bool:
        return (name or "").strip().upper() in self.relationship_types

    def dependency_rel_enums(self) -> list[RelationshipType]:
        return [RelationshipType(name) for name in self.dependency_relationship_types]

    def threat_enum(self) -> EntityType:
        return EntityType(self.threat_entity_type)

    def exposure_enums(self) -> list[EntityType]:
        return [EntityType(name) for name in self.exposure_entity_types]

    def coerce_relationship_type(self, name: str) -> Optional[str]:
        rel = (name or "").strip().upper()
        if rel in self.relationship_types:
            return rel
        if "RELATED_TO" in self.relationship_types:
            return "RELATED_TO"
        return None

    def coerce_attributes(self, entity_type: str, raw: dict[str, Any] | None) -> dict[str, Any]:
        spec = self.entity_types.get((entity_type or "").strip().lower())
        if not spec or not raw:
            return {}
        allowed = {attr.name: attr for attr in spec.attributes}
        cleaned: dict[str, Any] = {}
        for key, value in raw.items():
            attr = allowed.get(key)
            if attr is None or value is None or value == "":
                continue
            coerced = _coerce_value(attr, value)
            if coerced is not None:
                cleaned[key] = coerced
        return cleaned

    def prompt_block(self) -> str:
        lines = ["## Ontology schema (extract only these types)", "", "### Entity types"]
        for spec in self.entity_types.values():
            lines.append(f"- **{spec.name}**: {spec.description}")
            if spec.attributes:
                fields = []
                for attr in spec.attributes:
                    piece = f"{attr.name}:{attr.type}"
                    if attr.enum:
                        piece += f" enum={{{','.join(attr.enum)}}}"
                    fields.append(piece)
                lines.append(f"  attributes: {', '.join(fields)}")
        lines.append("")
        lines.append("### Relationship types")
        lines.append(", ".join(sorted(self.relationship_types)))
        return "\n".join(lines)

    def analysis_prompt_block(self) -> str:
        return (
            f"{self.prompt_block()}\n\n"
            "### Graph analysis\n"
            f"- Dependency edges (outgoing): {', '.join(self.dependency_relationship_types)}\n"
            f"- Threat entity type: {self.threat_entity_type}\n"
            f"- Exposure entity types: {', '.join(self.exposure_entity_types)}\n"
        )


def instance_gazetteer(store: Any, schema: OntologySchema) -> list[tuple[str, str, str]]:
    """(pattern, entity_id, entity_type) from schema-valid store instances."""
    if store is None:
        return []
    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity in store.all_entities():
        etype = entity.entity_type.value
        if not schema.is_entity_type(etype):
            continue
        candidates = {entity.id, entity.name, entity.id.replace("_", " ")}
        for raw in candidates:
            pattern = raw.strip().lower()
            if len(pattern) < 3:
                continue
            key = (pattern, entity.id)
            if key in seen:
                continue
            seen.add(key)
            entries.append((pattern, entity.id, etype))
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    return entries


def _parse_attribute(item: dict[str, Any]) -> AttributeSpec:
    raw_range = item.get("range")
    value_range = None
    if isinstance(raw_range, (list, tuple)) and len(raw_range) == 2:
        value_range = (float(raw_range[0]), float(raw_range[1]))
    enum = item.get("enum")
    return AttributeSpec(
        name=str(item["name"]),
        type=str(item.get("type", "string")),
        optional=bool(item.get("optional", True)),
        enum=tuple(str(v) for v in enum) if enum else None,
        value_range=value_range,
    )


def _coerce_value(attr: AttributeSpec, value: Any) -> Any:
    if attr.type == "float":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if attr.value_range and not (attr.value_range[0] <= number <= attr.value_range[1]):
            return None
        return number
    if attr.type.startswith("list"):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return None
        return [str(item) for item in value if item is not None and str(item).strip()]
    text = str(value).strip()
    if not text:
        return None
    if attr.enum and text not in attr.enum:
        return None
    return text


@lru_cache(maxsize=8)
def load_ontology_schema(path: str | Path | None = None) -> OntologySchema:
    schema_path = Path(path) if path else _SCHEMA_PATH
    payload = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}

    entity_types: dict[str, EntityTypeSpec] = {}
    for name, body in (payload.get("entity_types") or {}).items():
        EntityType(name)  # must exist in the Python enum
        body = body or {}
        attrs = tuple(_parse_attribute(item) for item in body.get("attributes") or [])
        entity_types[name] = EntityTypeSpec(
            name=name,
            description=str(body.get("description") or ""),
            attributes=attrs,
        )

    relationship_types = []
    for rel in payload.get("relationship_types") or []:
        rel_name = str(rel).strip().upper()
        RelationshipType(rel_name)
        relationship_types.append(rel_name)

    base_attributes = tuple(
        _parse_attribute(item) for item in payload.get("base_attributes") or []
    )
    if not entity_types or not relationship_types:
        raise ValueError(f"Invalid ontology schema: {schema_path}")

    analysis = payload.get("graph_analysis") or {}
    dependency_relationship_types = _subset_rel_types(
        analysis.get("dependency_relationship_types")
        or ["DEPENDS_ON", "SUPPLIES", "SUPPLIES_TO"],
        relationship_types,
        "graph_analysis.dependency_relationship_types",
    )
    threat_entity_type = str(analysis.get("threat_entity_type") or "threat").strip().lower()
    if threat_entity_type not in entity_types:
        raise ValueError(
            f"graph_analysis.threat_entity_type {threat_entity_type!r} is not an entity type"
        )
    exposure_raw = analysis.get("exposure_entity_types") or ["organization"]
    exposure_entity_types = tuple(
        _require_entity_type(name, entity_types, "graph_analysis.exposure_entity_types")
        for name in exposure_raw
    )

    return OntologySchema(
        entity_types=entity_types,
        relationship_types=frozenset(relationship_types),
        base_attributes=base_attributes,
        dependency_relationship_types=dependency_relationship_types,
        threat_entity_type=threat_entity_type,
        exposure_entity_types=exposure_entity_types,
    )


def _subset_rel_types(raw: list[Any], allowed: list[str], label: str) -> tuple[str, ...]:
    allowed_set = set(allowed)
    selected: list[str] = []
    for item in raw:
        name = str(item).strip().upper()
        if name not in allowed_set:
            raise ValueError(f"{label} contains unknown relationship type {name!r}")
        if name not in selected:
            selected.append(name)
    if not selected:
        raise ValueError(f"{label} must list at least one relationship type")
    return tuple(selected)


def _require_entity_type(name: Any, entity_types: dict[str, EntityTypeSpec], label: str) -> str:
    key = str(name).strip().lower()
    if key not in entity_types:
        raise ValueError(f"{label} contains unknown entity type {key!r}")
    return key
