"""Tests for the Threat Assessor agent."""

import pytest
from src.agents.osint_agent import OSINTResult
from src.agents.threat_agent import ThreatAssessorAgent, ThreatAssessmentResult


class TestThreatAssessorAgent:
    """Test threat assessment and risk scoring."""

    def test_run_returns_result(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait threat assessment")
        assert isinstance(result, ThreatAssessmentResult)

    def test_produces_risk_score(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert 0.0 <= result.overall_risk_score <= 1.0
        assert result.overall_risk_level in ["LOW", "GUARDED", "ELEVATED", "HIGH", "CRITICAL"]

    def test_produces_confidence(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert 0.0 <= result.confidence <= 1.0

    def test_has_threat_assessments(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert len(result.threat_assessments) > 0
        for ta in result.threat_assessments:
            assert "category" in ta
            assert "severity" in ta
            assert "confidence" in ta

    def test_has_historical_precedents(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert len(result.historical_precedents) > 0

    def test_has_escalation_indicators(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert len(result.escalation_indicators) > 0

    def test_has_mitigating_factors(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert len(result.mitigating_factors) > 0

    def test_key_findings(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        assert len(result.key_findings) > 0
        # First finding should mention risk level
        assert "risk" in result.key_findings[0].lower() or "CRITICAL" in result.key_findings[0] or "HIGH" in result.key_findings[0]

    def test_incorporates_osint_result(self, sample_osint_result):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait", osint_result=sample_osint_result)
        assert isinstance(result, ThreatAssessmentResult)
        # Should have escalation indicator mentioning sources
        source_indicators = [i for i in result.escalation_indicators if "sources" in i.lower()]
        assert len(source_indicators) > 0

    def test_incorporates_graph_result(self, sample_graph_result):
        agent = ThreatAssessorAgent()
        baseline = agent.run("Taiwan Strait")
        result = agent.run("Taiwan Strait", graph_result=sample_graph_result)
        assert isinstance(result, ThreatAssessmentResult)
        assert result.overall_risk_score == 0.95
        assert result.overall_risk_level == "CRITICAL"
        assert result.overall_risk_score != baseline.overall_risk_score
        assert result.risk_matrix["evidence"]["basis"] == "graph_exposure"
        assert any("high graph exposure" in item for item in result.escalation_indicators)
        assert any(item.startswith("Score basis:") for item in result.key_findings)
        assert "exposure >= 0.6" in result.threat_assessments[0]["assessment"]

    def test_threat_assertions_score_higher_than_comentions(self):
        agent = ThreatAssessorAgent()
        threat = OSINTResult(new_relationships=[
            {"type": "THREATENS", "confidence": 1.0, "source": "pla", "target": "tsmc"},
        ])
        comention = OSINTResult(new_relationships=[
            {"type": "RELATED_TO", "confidence": 1.0, "source": "pla", "target": "tsmc"},
        ])
        threat_result = agent.run("Taiwan Strait", osint_result=threat)
        weak_result = agent.run("Taiwan Strait", osint_result=comention)
        assert threat_result.overall_risk_score == 1.0
        assert weak_result.overall_risk_score == 0.35
        assert any("THREATENS" in item for item in threat_result.escalation_indicators)

    def test_blends_exposure_and_osint_assertions(self, sample_osint_result, sample_graph_result):
        sample_osint_result.new_relationships = [
            {"type": "THREATENS", "confidence": 0.5, "source": "blockade", "target": "kaohsiung"},
        ]
        result = ThreatAssessorAgent().run(
            "Taiwan Strait",
            osint_result=sample_osint_result,
            graph_result=sample_graph_result,
        )
        assert result.overall_risk_score == 0.77
        assert result.risk_matrix["evidence"]["basis"] == "graph_exposure+osint_assertions"
        assert result.risk_matrix["catalog_prior"] != result.overall_risk_score

    def test_result_serializable(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Taiwan Strait")
        d = result.to_dict()
        assert "overall_risk_score" in d
        assert "threat_assessments" in d

    def test_llm_writes_assessments(self, mock_llm):
        mock_llm.invoke.return_value.content = (
            "PLA posture is highly likely to remain coercive over the next 72 hours.\n"
            "Cyber access to port systems is a significant pre-positioning indicator."
        )
        agent = ThreatAssessorAgent(llm=mock_llm)
        result = agent.run("Taiwan Strait")
        assert mock_llm.invoke.called
        assert result.key_findings
        assert result.threat_assessments[0]["assessment"]

    def test_handles_missing_data(self):
        agent = ThreatAssessorAgent()
        result = agent.run("Unknown region with no data")
        assert isinstance(result, ThreatAssessmentResult)
        assert result.overall_risk_score >= 0
