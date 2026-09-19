"""Tests for config/ontology_schema.yaml as a runtime contract."""

from src.ontology.schema import EntityType, RelationshipType
from src.ontology.schema_def import load_ontology_schema


class TestOntologySchemaDef:
    def test_loads_yaml_entity_and_relationship_types(self):
        schema = load_ontology_schema()
        assert "organization" in schema.entity_types
        assert "threat" in schema.entity_types
        assert "RELATED_TO" in schema.relationship_types
        assert "DEPENDS_ON" in schema.relationship_types

    def test_yaml_types_match_python_enums(self):
        schema = load_ontology_schema()
        for name in schema.entity_types:
            assert EntityType(name).value == name
        for rel in schema.relationship_types:
            assert RelationshipType(rel).value == rel

    def test_rejects_unknown_entity_type(self):
        schema = load_ontology_schema()
        assert not schema.is_entity_type("spaceship")
        assert schema.is_entity_type("organization")
        assert schema.is_entity_type("Organization")

    def test_coerces_attributes_to_schema(self):
        schema = load_ontology_schema()
        cleaned = schema.coerce_attributes(
            "organization",
            {
                "org_type": "corporation",
                "country": "Taiwan",
                "sector": "semiconductor",
                "not_a_field": "drop me",
                "org_type_invalid": "megacorp",
            },
        )
        assert cleaned["org_type"] == "corporation"
        assert cleaned["country"] == "Taiwan"
        assert "not_a_field" not in cleaned

    def test_drops_invalid_enum_and_range(self):
        schema = load_ontology_schema()
        cleaned = schema.coerce_attributes(
            "threat",
            {"threat_type": "laser", "severity": "critical", "likelihood": 1.4},
        )
        assert "threat_type" not in cleaned
        assert cleaned["severity"] == "critical"
        assert "likelihood" not in cleaned

    def test_unknown_relationship_falls_back_to_related_to(self):
        schema = load_ontology_schema()
        assert schema.coerce_relationship_type("HUGS") == "RELATED_TO"
        assert schema.coerce_relationship_type("depends_on") == "DEPENDS_ON"

    def test_graph_analysis_block_is_schema_subset(self):
        schema = load_ontology_schema()
        assert schema.dependency_relationship_types == (
            "DEPENDS_ON",
            "SUPPLIES",
            "SUPPLIES_TO",
        )
        assert schema.threat_entity_type == "threat"
        assert schema.exposure_entity_types == ("organization",)
        for rel in schema.dependency_relationship_types:
            assert rel in schema.relationship_types
        assert schema.threat_entity_type in schema.entity_types

    def test_rejects_unknown_dependency_relationship(self, tmp_path):
        from pathlib import Path
        from src.ontology.schema_def import load_ontology_schema as load_schema

        src = Path("config/ontology_schema.yaml").read_text(encoding="utf-8")
        src = src.replace(
            "  dependency_relationship_types:\n    - DEPENDS_ON\n    - SUPPLIES\n    - SUPPLIES_TO",
            "  dependency_relationship_types:\n    - DEPENDS_ON\n    - SUPPLIES\n    - SUPPLIES_TO\n    - NOT_A_REL",
        )
        path = tmp_path / "bad_schema.yaml"
        path.write_text(src, encoding="utf-8")
        try:
            load_schema(path)
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "NOT_A_REL" in str(exc)
