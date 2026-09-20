from unittest.mock import Mock

from src.graph.graph_search_workflow import build_graph_search_workflow
from src.ontology.memory import MemoryBackend
from src.ontology.schema import Entity, EntityType
from src.ontology.schema_def import load_ontology_schema
from src.ontology.store import OntologyStore


class FakeNeo4jBackend(MemoryBackend):
    def graph_search_backend(self):
        return self

    def search_entity_candidates(
        self, query, entity_types=None, limit=20, embedding=None
    ):
        return super().search_entity_candidates(query, entity_types, limit)

    def execute_readonly(self, cypher, parameters, *, max_rows=200):
        return {
            "columns": ["entity"],
            "rows": [{"entity": {"id": "org1", "name": "Test Org"}}],
            "row_count": 1,
            "truncated": False,
            "audit": {"cypher_hash": "abc", "parameter_keys": ["id"], "duration_ms": 1},
        }


def test_agent_controls_multistep_graph_search(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = FakeNeo4jBackend()
    backend.add_entity(Entity(id="org1", name="Test Org", entity_type=EntityType.ORGANIZATION))
    store = OntologyStore(backend)
    llm = Mock()
    llm.invoke.side_effect = [
        Mock(content='{"question_type":"impact","mentions":[{"mention_id":"m1",'
                     '"text":"Test Org","expected_types":["organization"]}],'
                     '"answer_goal":"find evidence"}'),
        Mock(content='{"action":"query","goal":"inspect entity",'
                     '"cypher":"MATCH (n:Entity) WHERE n.id = $id RETURN n LIMIT $limit",'
                     '"parameters":{"id":"org1","limit":10}}'),
        Mock(content='{"action":"finish","reason":"evidence sufficient",'
                     '"summary":"Test Org was found in the graph."}'),
    ]
    workflow = build_graph_search_workflow(store, llm, load_ontology_schema())
    result = workflow.invoke({"query": "What affects Test Org?"})["result"]
    assert result["schema_version"] == "agentic-graph-search-v1"
    assert result["linked_entities"][0]["entity_id"] == "org1"
    assert len(result["search_trace"]) == 1
    assert result["stop_reason"] == "evidence sufficient"
