"""Graph Analyst Agent - Ontology traversal, relationship discovery, and exposure analysis.

LIVE: Traverses the ontology store to find dependency chains, calculate exposure scores,
identify critical paths, and analyze supply chain relationships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.ontology.schema_def import OntologySchema, instance_gazetteer, load_ontology_schema
from src.ontology.store import OntologyStore
from src.tools.ontology_tools import (
    find_dependency_chains,
    get_exposure_report,
    find_shortest_path,
    traverse_entity,
)


@dataclass
class GraphAnalysisResult:
    """Result from graph analysis run."""
    query: str = ""
    dependency_chains: list[list[dict[str, str]]] = field(default_factory=list)
    exposure_scores: list[dict[str, Any]] = field(default_factory=list)
    critical_paths: list[dict[str, Any]] = field(default_factory=list)
    hub_entities: list[dict[str, Any]] = field(default_factory=list)
    traversal_stats: dict[str, Any] = field(default_factory=dict)
    key_findings: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "dependency_chains": self.dependency_chains,
            "exposure_scores": self.exposure_scores,
            "critical_paths": self.critical_paths,
            "hub_entities": self.hub_entities,
            "traversal_stats": self.traversal_stats,
            "key_findings": self.key_findings,
            "timestamp": self.timestamp,
        }


class GraphAnalystAgent:
    """Graph analysis agent for ontology traversal and relationship discovery.

    Analyzes the ontology graph to find supply chain dependencies, calculate
    exposure to threats, identify critical paths and hub entities.
    """

    def __init__(
        self,
        ontology_store: Optional[OntologyStore] = None,
        llm: Any = None,
        schema: Optional[OntologySchema] = None,
    ):
        self.store = ontology_store
        self.llm = llm
        self.schema = schema or load_ontology_schema()

    def run(self, query: str) -> GraphAnalysisResult:
        """Execute graph analysis for a given query.

        Args:
            query: Analysis query describing what to analyze.

        Returns:
            GraphAnalysisResult with dependency chains, exposure scores, etc.
        """
        if not self.store:
            return GraphAnalysisResult(query=query, key_findings=["No ontology store available"])

        result = GraphAnalysisResult(query=query)

        # Step 1: Identify key entities to analyze
        key_entity_ids = self._identify_focus_entities(query)

        # Step 2: Find dependency chains
        for eid in key_entity_ids:
            chains = find_dependency_chains(
                self.store,
                eid,
                max_depth=5,
                rel_types=self.schema.dependency_rel_enums(),
            )
            result.dependency_chains.extend(chains)

        result.exposure_scores = get_exposure_report(
            self.store,
            entity_types=self.schema.exposure_enums(),
            threat_entity_type=self.schema.threat_enum(),
        )

        # Step 4: Find critical paths between key entities
        result.critical_paths = self._find_critical_paths(key_entity_ids)

        # Step 5: Identify hub entities (highest connectivity)
        result.hub_entities = self._find_hub_entities()

        # Step 6: Traversal statistics
        result.traversal_stats = self._compute_traversal_stats(key_entity_ids)

        # Step 7: Generate key findings
        result.key_findings = self._generate_findings(result)

        return result

    def _identify_focus_entities(self, query: str) -> list[str]:
        """Focus on schema-valid store instances mentioned in the query."""
        query_lower = query.lower()
        focus_ids: list[str] = []
        for pattern, entity_id, _etype in instance_gazetteer(self.store, self.schema):
            if pattern in query_lower and entity_id not in focus_ids:
                focus_ids.append(entity_id)
        if focus_ids:
            return focus_ids[:8]
        return self._default_focus_ids()

    def _default_focus_ids(self, limit: int = 3) -> list[str]:
        ranked: list[tuple[int, str]] = []
        for entity in self.store.all_entities():
            if not self.schema.is_entity_type(entity.entity_type.value):
                continue
            ranked.append((len(self.store.get_neighbors(entity.id)), entity.id))
        ranked.sort(reverse=True)
        return [entity_id for _degree, entity_id in ranked[:limit]]

    def _find_critical_paths(self, entity_ids: list[str]) -> list[dict[str, Any]]:
        """Find shortest paths between all pairs of focus entities."""
        paths: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        # Also include threats
        threat_ids = [
            e.id for e in self.store.query_by_type(self.schema.threat_enum())
        ]
        all_ids = list(set(entity_ids + threat_ids))

        for i, src in enumerate(all_ids):
            for tgt in all_ids[i + 1:]:
                pair = tuple(sorted([src, tgt]))
                if pair in seen:
                    continue
                seen.add(pair)
                path = find_shortest_path(self.store, src, tgt)
                if path and len(path) > 1:
                    src_entity = self.store.get_entity(src)
                    tgt_entity = self.store.get_entity(tgt)
                    paths.append({
                        "source": src,
                        "source_name": src_entity.name if src_entity else src,
                        "target": tgt,
                        "target_name": tgt_entity.name if tgt_entity else tgt,
                        "path": path,
                        "hops": len(path) - 1,
                    })

        paths.sort(key=lambda p: p["hops"])
        return paths[:20]

    def _find_hub_entities(self, top_n: int = 10) -> list[dict[str, Any]]:
        """Find entities with highest connectivity (degree centrality)."""
        degree_counts: dict[str, int] = {}
        for entity in self.store.all_entities():
            if not self.schema.is_entity_type(entity.entity_type.value):
                continue
            neighbors = self.store.get_neighbors(entity.id)
            degree_counts[entity.id] = len(neighbors)

        sorted_entities = sorted(degree_counts.items(), key=lambda x: x[1], reverse=True)
        hubs = []
        for eid, degree in sorted_entities[:top_n]:
            entity = self.store.get_entity(eid)
            if entity:
                hubs.append({
                    "id": eid,
                    "name": entity.name,
                    "type": entity.entity_type.value,
                    "degree": degree,
                    "description": entity.description[:100],
                })
        return hubs

    def _compute_traversal_stats(self, entity_ids: list[str]) -> dict[str, Any]:
        """Compute traversal statistics for focus entities."""
        stats: dict[str, Any] = {
            "total_entities": self.store.entity_count,
            "total_relationships": self.store.relationship_count,
            "focus_entities": len(entity_ids),
            "traversals": [],
        }

        for eid in entity_ids[:5]:
            traversal = traverse_entity(self.store, eid, hops=3)
            entity = self.store.get_entity(eid)
            stats["traversals"].append({
                "entity": entity.name if entity else eid,
                "reachable_entities_3_hop": traversal["entity_count"],
                "relationships_traversed": traversal["relationship_count"],
                "paths_found": len(traversal["paths"]),
            })

        return stats

    def _generate_findings(self, result: GraphAnalysisResult) -> list[str]:
        """Generate human-readable key findings from the analysis."""
        if self.llm:
            from src.agents.llm_support import bullet_lines, invoke_llm, load_prompt
            hubs = ", ".join(
                f"{h['name']} (degree {h['degree']})" for h in result.hub_entities[:5]
            )
            high_exposure = [
                e for e in result.exposure_scores if e.get("exposure_score", 0) >= 0.6
            ]
            raw = invoke_llm(
                self.llm,
                load_prompt("graph_analyst") + "\n\n" + self.schema.analysis_prompt_block(),
                "Write up to 5 actionable intelligence findings from this graph analysis. "
                "Return one finding per line.\n\n"
                f"Query: {result.query}\n"
                f"Entities: {result.traversal_stats.get('total_entities', 0)}\n"
                f"Relationships: {result.traversal_stats.get('total_relationships', 0)}\n"
                f"Hubs: {hubs or 'none'}\n"
                f"High-exposure entities: {len(high_exposure)}\n"
                f"Dependency chains: {len(result.dependency_chains)}\n"
                f"Critical paths: {len(result.critical_paths)}",
            )
            findings = bullet_lines(raw, limit=8)
            if findings:
                return findings

        findings: list[str] = []

        # Finding: Hub entities
        if result.hub_entities:
            top_hub = result.hub_entities[0]
            findings.append(
                f"{top_hub['name']} is the most connected entity with {top_hub['degree']} "
                f"relationships, making it a critical node in the intelligence graph."
            )

        # Finding: Exposure scores
        high_exposure = [e for e in result.exposure_scores if e["exposure_score"] >= 0.6]
        if high_exposure:
            names = ", ".join(e["name"] for e in high_exposure[:5])
            findings.append(
                f"{len(high_exposure)} entities have high threat exposure (>0.6): {names}."
            )

        # Finding: Dependency chains
        if result.dependency_chains:
            longest = max(result.dependency_chains, key=len)
            chain_names = " -> ".join(node["name"] for node in longest)
            findings.append(
                f"Longest dependency chain ({len(longest)} nodes): {chain_names}."
            )

        # Finding: Critical paths
        short_paths = [p for p in result.critical_paths if p["hops"] <= 2]
        if short_paths:
            findings.append(
                f"{len(short_paths)} critical paths with 2 or fewer hops between "
                f"key entities and threats, indicating direct exposure."
            )

        # Finding: Total graph stats
        findings.append(
            f"Ontology graph contains {result.traversal_stats.get('total_entities', 0)} entities "
            f"and {result.traversal_stats.get('total_relationships', 0)} relationships."
        )

        return findings
