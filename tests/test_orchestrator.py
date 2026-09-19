"""Tests for the Orchestrator."""

import pytest
from src.orchestrator import Orchestrator
from src.agents.llm_support import MissingOpenAIKeyError


class TestOrchestrator:
    """Test the orchestrator initialization and execution."""

    def test_init_default(self, monkeypatch):
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        orch = Orchestrator()
        assert orch.model_name == "gpt-4o-mini"

    def test_init_custom_model(self):
        orch = Orchestrator(model_name="gpt-4o")
        assert orch.model_name == "gpt-4o"

    def test_workflow_builds(self):
        orch = Orchestrator()
        workflow = orch.workflow
        assert workflow is not None

    def test_run_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        orch = Orchestrator()
        with pytest.raises(MissingOpenAIKeyError, match="OPENAI_API_KEY"):
            orch.run("Taiwan Strait")

    def test_run_stream_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        orch = Orchestrator()
        with pytest.raises(MissingOpenAIKeyError, match="OPENAI_API_KEY"):
            next(orch.run_stream("Taiwan Strait"))

    def test_run_executes(self, mock_llm):
        """Test that run() executes the full workflow."""
        orch = Orchestrator(llm=mock_llm)
        result = orch.run("Taiwan Strait supply chain disruption")

        assert result.get("status") != "error", result.get("error")
        assert "osint_results" in result
        assert "graph_results" in result
        assert "threat_results" in result
        assert "briefing" in result
        assert mock_llm.invoke.called

    def test_run_populates_osint(self, mock_llm):
        orch = Orchestrator(llm=mock_llm)
        result = orch.run("Taiwan Strait")
        osint = result.get("osint_results", {})
        assert osint.get("sources_consulted", 0) > 0

    def test_run_populates_graph(self, mock_llm):
        orch = Orchestrator(llm=mock_llm)
        result = orch.run("Taiwan Strait supply chain")
        graph = result.get("graph_results", {})
        assert len(graph.get("exposure_scores", [])) > 0

    def test_run_populates_threat(self, mock_llm):
        orch = Orchestrator(llm=mock_llm)
        result = orch.run("Taiwan Strait")
        threat = result.get("threat_results", {})
        assert threat.get("overall_risk_score", 0) > 0

    def test_run_populates_briefing(self, mock_llm):
        orch = Orchestrator(llm=mock_llm)
        result = orch.run("Taiwan Strait")
        briefing = result.get("briefing", {})
        assert len(briefing.get("briefing_text", "")) > 0
        assert briefing.get("metadata", {}).get("generation_method") == "llm"

    def test_run_has_timeline(self, mock_llm):
        orch = Orchestrator(llm=mock_llm)
        result = orch.run("Taiwan Strait")
        timeline = result.get("timeline", [])
        assert len(timeline) > 0

    def test_run_stream(self, mock_llm):
        orch = Orchestrator(llm=mock_llm)
        events = list(orch.run_stream("Taiwan Strait"))
        assert len(events) > 0
        node_names = [name for name, _ in events]
        assert "coordinator" in node_names
        assert "briefing_drafter" in node_names
