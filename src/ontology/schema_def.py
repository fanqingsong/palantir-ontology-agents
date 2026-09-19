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

    def is_entity_type(self, name: str) -> bool:
        return (name or "").strip().lower() in self.entity_types

    def is_relationship_type(self, name: str) -> bool:
        return (name or "").strip().upper() in self.relationship_types

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


@lru_cache(maxsize=1)
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
    return OntologySchema(
        entity_types=entity_types,
        relationship_types=frozenset(relationship_types),
        base_attributes=base_attributes,
    )
