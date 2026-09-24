"""Threat Assessor Agent - Risk scoring and threat assessment.

SCAFFOLDED: Uses mock threat intelligence data for demo purposes.
In production, this agent would:
- Query classified threat intelligence databases (e.g., Palantir Gotham threat feeds)
- Cross-reference OSINT findings with SIGINT/IMINT/HUMINT sources
- Apply ML-based threat scoring models trained on historical conflict data
- Generate MITRE ATT&CK mappings for cyber threats
- Produce assessments with proper IC confidence language (likely, highly likely, etc.)
- Apply classification markings based on source material

Integration points:
- ThreatIntelligenceAPI: Replace mock data with real threat feed connector
- HistoricalAnalysisEngine: Replace mock precedents with ML-powered precedent matching
- ClassificationEngine: Add automatic classification marking based on sources used
- ConfidenceCalibration: Add calibrated confidence scoring model
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.agents.osint_agent import OSINTResult
from src.agents.graph_agent import GraphAnalysisResult
from src.tools.threat_tools import (
    ThreatIntelligence,
    get_threat_intelligence,
    get_historical_precedents,
    calculate_risk_matrix,
)


@dataclass
class ThreatAssessmentResult:
    """Result from threat assessment run."""
    query: str = ""
    overall_risk_score: float = 0.0
    overall_risk_level: str = "unknown"
    confidence: float = 0.0
    threat_assessments: list[dict[str, Any]] = field(default_factory=list)
    risk_matrix: dict[str, Any] = field(default_factory=dict)
    historical_precedents: list[dict[str, Any]] = field(default_factory=list)
    escalation_indicators: list[str] = field(default_factory=list)
    mitigating_factors: list[str] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "overall_risk_score": self.overall_risk_score,
            "overall_risk_level": self.overall_risk_level,
            "confidence": self.confidence,
            "threat_assessments": self.threat_assessments,
            "risk_matrix": self.risk_matrix,
            "historical_precedents": self.historical_precedents,
            "escalation_indicators": self.escalation_indicators,
            "mitigating_factors": self.mitigating_factors,
            "key_findings": self.key_findings,
            "timestamp": self.timestamp,
        }


_THREAT_RELATIONS = {"THREATENS"}
_DEPENDENCY_RELATIONS = {"DEPENDS_ON", "SUPPLIES", "SUPPLIES_TO"}


def _relation_type(item: dict[str, Any]) -> str:
    raw = item.get("type") or item.get("predicate") or ""
    if hasattr(raw, "value"):
        raw = raw.value
    return str(raw).upper()


def _unit_interval(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _score_run_evidence(
    osint_result: Optional[OSINTResult],
    graph_result: Optional[GraphAnalysisResult],
) -> tuple[Optional[float], dict[str, Any]]:
    """Score this run from graph exposure and OSINT assertions.

    Exposure uses the mean of the upper half of scores so the closest
    entities dominate. Assertions weight THREATENS above supply edges and
    other co-mentions. When both exist, exposure is 60% of the score.
    """
    exposure_values = [
        _unit_interval(item.get("exposure_score"))
        for item in (graph_result.exposure_scores if graph_result else [])
        if isinstance(item, dict)
    ]
    assertions = [
        item for item in (osint_result.new_relationships if osint_result else [])
        if isinstance(item, dict)
    ]
    evidence: dict[str, Any] = {}

    if exposure_values:
        ranked = sorted(exposure_values, reverse=True)
        top = ranked[: max(1, (len(ranked) + 1) // 2)]
        evidence["graph_exposure"] = round(sum(top) / len(top), 4)
        evidence["exposure_count"] = len(exposure_values)
        evidence["high_exposure_count"] = sum(1 for value in exposure_values if value >= 0.6)

    if assertions:
        weighted: list[float] = []
        threat_count = 0
        for item in assertions:
            rel_type = _relation_type(item)
            confidence = _unit_interval(item.get("confidence"), default=0.5)
            if rel_type in _THREAT_RELATIONS:
                weight = 1.0
                threat_count += 1
            elif rel_type in _DEPENDENCY_RELATIONS:
                weight = 0.7
            else:
                weight = 0.35
            weighted.append(confidence * weight)
        evidence["osint_assertion"] = round(sum(weighted) / len(weighted), 4)
        evidence["assertion_count"] = len(assertions)
        evidence["threat_assertion_count"] = threat_count

    if "graph_exposure" in evidence and "osint_assertion" in evidence:
        score = 0.6 * evidence["graph_exposure"] + 0.4 * evidence["osint_assertion"]
        evidence["basis"] = "graph_exposure+osint_assertions"
    elif "graph_exposure" in evidence:
        score = evidence["graph_exposure"]
        evidence["basis"] = "graph_exposure"
    elif "osint_assertion" in evidence:
        score = evidence["osint_assertion"]
        evidence["basis"] = "osint_assertions"
    else:
        return None, {}

    evidence["score"] = round(score, 2)
    return evidence["score"], evidence


def _evidence_finding(result: ThreatAssessmentResult) -> str:
    evidence = result.risk_matrix.get("evidence") or {}
    if not evidence:
        return ""
    return (
        "Score basis: "
        f"{evidence.get('basis')} "
        f"(graph exposure {evidence.get('graph_exposure', 'n/a')}, "
        f"OSINT assertions {evidence.get('assertion_count', 0)})"
    )


def _evidence_confidence(
    osint_result: Optional[OSINTResult],
    graph_result: Optional[GraphAnalysisResult],
) -> Optional[float]:
    """Confidence follows how much of this run is actually evidenced."""
    parts: list[float] = []
    assertions = [
        item for item in (osint_result.new_relationships if osint_result else [])
        if isinstance(item, dict)
    ]
    if assertions:
        parts.append(sum(_unit_interval(item.get("confidence"), 0.5) for item in assertions) / len(assertions))
    exposure_count = len(graph_result.exposure_scores) if graph_result else 0
    if exposure_count:
        parts.append(min(1.0, 0.5 + 0.05 * min(exposure_count, 10)))
    if not parts:
        return None
    return round(sum(parts) / len(parts), 2)


class ThreatAssessorAgent:
    """Threat assessment agent for risk scoring and threat analysis.

    Overall risk follows this run's graph exposure and OSINT assertions.
    The sample threat catalog remains the prior when a run has neither,
    and still supplies category narratives until a live feed is connected.
    """

    def __init__(self, llm: Any = None):
        self.llm = llm

    def run(self, query: str,
            osint_result: Optional[OSINTResult] = None,
            graph_result: Optional[GraphAnalysisResult] = None) -> ThreatAssessmentResult:
        """Execute threat assessment.

        Args:
            query: Assessment query.
            osint_result: Optional OSINT findings to incorporate.
            graph_result: Optional graph analysis to incorporate.

        Returns:
            ThreatAssessmentResult with risk scores and assessments.
        """
        result = ThreatAssessmentResult(query=query)

        # Step 1: Catalog is context. The numeric score comes from this run.
        threat_intel = get_threat_intelligence("taiwan_strait")

        # Step 2: Prefer graph exposure and OSINT assertions over the catalog prior.
        result.risk_matrix = calculate_risk_matrix(threat_intel)
        catalog_prior = result.risk_matrix["overall_risk_score"]
        evidence_score, evidence = _score_run_evidence(osint_result, graph_result)
        if evidence_score is not None:
            result.risk_matrix["catalog_prior"] = catalog_prior
            result.risk_matrix["evidence"] = evidence
            result.risk_matrix["overall_risk_score"] = evidence_score
            result.overall_risk_score = evidence_score
            evidence_confidence = _evidence_confidence(osint_result, graph_result)
            if evidence_confidence is not None:
                result.confidence = evidence_confidence
        else:
            result.overall_risk_score = catalog_prior
        result.overall_risk_level = self._score_to_level(result.overall_risk_score)

        # Step 3: Get historical precedents
        result.historical_precedents = get_historical_precedents("strait_crisis")

        # Step 4: Build individual threat assessments
        for ti in threat_intel:
            assessment = {
                "threat_id": ti.threat_id,
                "category": ti.category,
                "severity": ti.severity,
                "confidence": ti.confidence,
                "description": ti.description,
                "indicators": ti.indicators,
                "assessment": self._assess_individual_threat(ti, osint_result, graph_result, query),
            }
            result.threat_assessments.append(assessment)

        # Step 5: Catalog confidence applies only when this run has no evidence.
        if "evidence" not in result.risk_matrix:
            confidences = [ti.confidence for ti in threat_intel]
            result.confidence = round(sum(confidences) / max(len(confidences), 1), 2)

        # Step 6: Identify escalation indicators and mitigating factors
        result.escalation_indicators = self._identify_escalation_indicators(
            threat_intel, osint_result, graph_result
        )
        result.mitigating_factors = self._identify_mitigating_factors(threat_intel, graph_result)

        # Step 7: Key findings
        result.key_findings = self._generate_findings(result)

        return result

    def _score_to_level(self, score: float) -> str:
        """Convert numeric risk score to categorical level."""
        if score >= 0.8:
            return "CRITICAL"
        elif score >= 0.6:
            return "HIGH"
        elif score >= 0.4:
            return "ELEVATED"
        elif score >= 0.2:
            return "GUARDED"
        else:
            return "LOW"

    def _assess_individual_threat(self, ti: ThreatIntelligence,
                                   osint_result: Optional[OSINTResult],
                                   graph_result: Optional[GraphAnalysisResult],
                                   query: str = "") -> str:
        """Generate assessment text for an individual threat."""
        if self.llm:
            from src.agents.llm_support import invoke_llm, load_prompt
            osint_bits = "; ".join((osint_result.key_findings[:3] if osint_result else []) or [])
            graph_bits = "; ".join((graph_result.key_findings[:3] if graph_result else []) or [])
            return invoke_llm(
                self.llm,
                load_prompt("threat_assessor"),
                "Write a 2-4 sentence intelligence assessment for this threat. "
                "Use IC confidence language.\n\n"
                f"Query: {query}\n"
                f"Category: {ti.category}\n"
                f"Severity: {ti.severity}\n"
                f"Confidence: {ti.confidence}\n"
                f"Description: {ti.description}\n"
                f"Indicators: {', '.join(ti.indicators)}\n"
                f"OSINT: {osint_bits or 'n/a'}\n"
                f"Graph: {graph_bits or 'n/a'}",
            )

        assessments = {
            "military_buildup": (
                "PLA force posture is consistent with coercive military signaling, with indicators "
                "suggesting readiness for escalation to blockade operations. Current assessment: "
                "exercises are primarily signaling but maintain real escalation potential."
            ),
            "cyber_operations": (
                "Cyber operations targeting Taiwan critical infrastructure represent a serious "
                "pre-positioning effort. The combination of port system intrusions and SCADA "
                "network access indicates preparation for potential destructive operations "
                "synchronized with kinetic military activity."
            ),
            "economic_coercion": (
                "Economic pressure campaign is coordinated and multi-vector. Export control "
                "pressure on semiconductor equipment supply chain represents long-term strategic "
                "threat to Taiwan's technology advantage. Current impact is moderate but escalation "
                "pathways exist."
            ),
            "gray_zone_operations": (
                "Maritime gray zone operations are designed to normalize PLA presence and erode "
                "freedom of navigation in the Taiwan Strait. The 400% increase in close approach "
                "incidents significantly raises risk of accidental escalation."
            ),
        }
        return assessments.get(ti.category, f"Assessment pending for {ti.category} threat category.")

    def _identify_escalation_indicators(self, threat_intel: list[ThreatIntelligence],
                                         osint_result: Optional[OSINTResult],
                                         graph_result: Optional[GraphAnalysisResult] = None) -> list[str]:
        """Identify factors that could lead to escalation."""
        indicators = [
            "PLA exercise scale exceeds previous demonstrations (Joint Sword-2026A)",
            "Simultaneous cyber and kinetic preparation indicators",
            "Commercial shipping disruption creating economic pressure on Taiwan",
            "Dual carrier deployment (Liaoning + Shandong) not seen since 2022",
        ]

        if osint_result and osint_result.sources_consulted > 3:
            indicators.append(
                f"Elevated media coverage ({osint_result.sources_consulted} sources) "
                "indicates sustained international attention"
            )

        threat_assertions = [
            item for item in (osint_result.new_relationships if osint_result else [])
            if isinstance(item, dict) and _relation_type(item) in _THREAT_RELATIONS
        ]
        if threat_assertions:
            indicators.append(
                f"{len(threat_assertions)} OSINT assertion(s) state a THREATENS relationship"
            )

        if graph_result and graph_result.exposure_scores:
            high_exposure = sum(
                1 for item in graph_result.exposure_scores
                if isinstance(item, dict) and _unit_interval(item.get("exposure_score")) >= 0.6
            )
            if high_exposure:
                indicators.append(
                    f"{high_exposure} entities sit at high graph exposure (>= 0.6) to a recorded threat"
                )

        return indicators

    def _identify_mitigating_factors(self, threat_intel: list[ThreatIntelligence],
                                      graph_result: Optional[GraphAnalysisResult]) -> list[str]:
        """Identify factors that reduce escalation risk."""
        factors = [
            "US carrier strike group deployment provides deterrent signaling",
            "No indicators of PLA mobilization beyond exercise participants",
            "Diplomatic channels remain open (back-channel communications reported)",
            "PLA exercises have defined end date (March 22), suggesting limited scope",
            "Economic interdependence creates mutual costs for sustained disruption",
        ]

        if graph_result and graph_result.exposure_scores:
            high_exposure = sum(
                1 for item in graph_result.exposure_scores
                if isinstance(item, dict) and _unit_interval(item.get("exposure_score")) >= 0.6
            )
            if high_exposure == 0:
                factors.append(
                    "Graph exposure for this run stays below 0.6"
                )

        return factors

    def _generate_findings(self, result: ThreatAssessmentResult) -> list[str]:
        """Generate key findings from the threat assessment."""
        if self.llm:
            from src.agents.llm_support import bullet_lines, invoke_llm, load_prompt
            raw = invoke_llm(
                self.llm,
                load_prompt("threat_assessor"),
                "Write up to 5 key findings for this threat assessment. "
                "Return one finding per line.\n\n"
                f"Query: {result.query}\n"
                f"Risk level: {result.overall_risk_level}\n"
                f"Risk score: {result.overall_risk_score}\n"
                f"Confidence: {result.confidence}\n"
                f"Threats: {', '.join(ta.get('category', '') for ta in result.threat_assessments)}\n"
                f"Escalation indicators: {len(result.escalation_indicators)}\n"
                f"Mitigating factors: {len(result.mitigating_factors)}\n"
                f"Evidence: {result.risk_matrix.get('evidence') or 'catalog prior'}",
            )
            findings = bullet_lines(raw, limit=8)
            if findings:
                evidence_line = _evidence_finding(result)
                if evidence_line:
                    findings.append(evidence_line)
                return findings

        findings = [
            f"Overall risk level: {result.overall_risk_level} "
            f"(score: {result.overall_risk_score:.2f}, confidence: {result.confidence:.2f})",
        ]
        evidence_line = _evidence_finding(result)
        if evidence_line:
            findings.append(evidence_line)

        critical_threats = [ta for ta in result.threat_assessments if ta["severity"] == "critical"]
        if critical_threats:
            findings.append(
                f"{len(critical_threats)} CRITICAL threat(s) identified: "
                + ", ".join(ta["category"] for ta in critical_threats)
            )

        if result.historical_precedents:
            most_relevant = max(result.historical_precedents, key=lambda p: p["relevance_score"])
            findings.append(
                f"Most relevant historical precedent: {most_relevant['event']} "
                f"(relevance: {most_relevant['relevance_score']:.2f})"
            )

        findings.append(
            f"{len(result.escalation_indicators)} escalation indicators vs "
            f"{len(result.mitigating_factors)} mitigating factors identified"
        )

        return findings
