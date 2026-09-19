"""Tests for the OSINT Collector agent."""

import pytest
from src.agents.osint_agent import OSINTAgent, OSINTResult


class TestOSINTAgent:
    """Test OSINT collection and entity extraction."""

    def test_run_returns_result(self, sample_ontology_store):
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        result = agent.run("Taiwan Strait supply chain disruption")
        assert isinstance(result, OSINTResult)

    def test_finds_search_results(self, sample_ontology_store):
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        result = agent.run("Taiwan Strait")
        assert result.sources_consulted > 0
        assert len(result.search_results) > 0

    def test_extracts_entities(self, sample_ontology_store):
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        result = agent.run("Taiwan Strait supply chain disruption")
        assert len(result.extracted_entities) > 0

        entity_ids = [e["id"] for e in result.extracted_entities]
        assert "tsmc" in entity_ids or "taiwan_strait" in entity_ids

    def test_extracts_key_findings(self, sample_ontology_store):
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        result = agent.run("Taiwan Strait supply chain disruption")
        assert len(result.key_findings) > 0

    def test_result_serializable(self, sample_ontology_store):
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        result = agent.run("Taiwan Strait")
        d = result.to_dict()
        assert "query" in d
        assert "search_results" in d
        assert "extracted_entities" in d

    def test_runs_without_store(self):
        agent = OSINTAgent()
        result = agent.run("Taiwan Strait")
        assert isinstance(result, OSINTResult)
        assert result.sources_consulted > 0

    def test_multiple_search_queries_generated(self, sample_ontology_store):
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        queries = agent._generate_search_queries("Taiwan Strait supply chain")
        assert len(queries) >= 2

    def test_entity_extraction_from_results(self, sample_ontology_store):
        from src.tools.web_search import SearchResult
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        results = [
            SearchResult(title="TSMC production update", url="http://test.com",
                         content="TSMC in Taiwan Strait region faces challenges from PLA exercises.", score=0.9)
        ]
        entities = agent._extract_entities(results)
        ids = [e["id"] for e in entities]
        assert "tsmc" in ids

    def test_llm_generates_search_queries_and_findings(self, sample_ontology_store, mock_llm):
        mock_llm.invoke.return_value.content = (
            "taiwan strait blockade shipping\n"
            "TSMC supply chain delay\n"
            "PLA launched Joint Sword exercises around Taiwan disrupting Kaohsiung port traffic."
        )
        agent = OSINTAgent(ontology_store=sample_ontology_store, llm=mock_llm)
        result = agent.run("Taiwan Strait supply chain disruption")
        assert mock_llm.invoke.called
        assert result.key_findings
        assert any("Joint Sword" in finding or "taiwan" in finding.lower() for finding in result.key_findings)

    def test_writes_relationships_into_store(self, sample_ontology_store):
        before = sample_ontology_store.relationship_count
        agent = OSINTAgent(ontology_store=sample_ontology_store)
        result = agent.run("Taiwan Strait supply chain disruption")
        if result.new_relationships:
            assert sample_ontology_store.relationship_count >= before
            written = [rel for rel in result.new_relationships if sample_ontology_store.get_relationship(f"osint_{rel['source']}_{rel['target']}")]
            assert written

    def test_drops_entity_types_outside_schema(self, sample_ontology_store, mock_llm):
        mock_llm.invoke.return_value.content = """
        {"entities": [
            {"name": "TSMC", "type": "organization", "canonical_id": "tsmc",
             "attributes": {"org_type": "corporation", "country": "Taiwan", "foo": "nope"}},
            {"name": "Death Star", "type": "spaceship"}
        ], "relationships": [
            {"source": "tsmc", "target": "taiwan_strait", "type": "DEPENDS_ON"},
            {"source": "tsmc", "target": "taiwan_strait", "type": "HUGS"}
        ]}
        """
        agent = OSINTAgent(ontology_store=sample_ontology_store, llm=mock_llm)
        from src.tools.web_search import SearchResult
        results = [
            SearchResult(
                title="TSMC production update",
                url="http://test.com",
                content="TSMC in Taiwan Strait region faces challenges.",
                score=0.9,
            )
        ]
        entities, relationships = agent._extract_with_schema(results)
        types = {e["type"] for e in entities}
        ids = {e["id"] for e in entities}
        assert "spaceship" not in types
        assert "tsmc" in ids
        tsmc = next(e for e in entities if e["id"] == "tsmc")
        assert tsmc["attributes"]["org_type"] == "corporation"
        assert "foo" not in tsmc["attributes"]
        rel_types = {r["type"] for r in relationships}
        assert "DEPENDS_ON" in rel_types
        assert "HUGS" not in rel_types
        assert "RELATED_TO" in rel_types

    def test_writes_schema_typed_relationship(self, sample_ontology_store, mock_llm):
        mock_llm.invoke.side_effect = [
            type("Msg", (), {"content": "taiwan strait blockade shipping\nTSMC supply chain delay\nPLA navy western pacific"})(),
            type("Msg", (), {"content": '{"entities":[{"name":"TSMC","type":"organization","canonical_id":"tsmc"}],"relationships":[{"source":"tsmc","target":"taiwan_strait","type":"DEPENDS_ON","confidence":0.9}]}'})(),
            type("Msg", (), {"content": "PLA launched Joint Sword exercises around Taiwan disrupting Kaohsiung port traffic."})(),
        ]
        agent = OSINTAgent(ontology_store=sample_ontology_store, llm=mock_llm)
        result = agent.run("Taiwan Strait supply chain disruption")
        assert sample_ontology_store.get_relationship("osint_tsmc_taiwan_strait_DEPENDS_ON")

    def test_gazetteer_uses_store_instances_not_hardcoded_aliases(self, minimal_store):
        from src.tools.web_search import SearchResult
        agent = OSINTAgent(ontology_store=minimal_store)
        results = [
            SearchResult(
                title="Test Org at Test Port",
                url="http://example.com",
                content="Test Org operates Test Port under Test Threat pressure.",
                score=0.91,
            )
        ]
        ids = {e["id"] for e in agent._extract_entities(results)}
        assert ids == {"org1", "loc1", "threat1"}
