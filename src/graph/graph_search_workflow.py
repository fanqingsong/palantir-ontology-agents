"""LangGraph subgraph for autonomous, bounded Neo4j search."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.agents.llm_support import invoke_llm
from src.entity_linking.models import Mention
from src.entity_linking.normalizer import normalize_surface
from src.entity_linking.service import EntityLinkingService
from src.ontology.graph_ops import degree_by_entity
from src.ontology.schema_def import OntologySchema
from src.ontology.store import OntologyStore
from src.tools.graph_search_tools import (
    describe_graph_schema,
    inspect_entity,
    run_readonly_cypher,
)
from src.tools.ontology_tools import find_dependency_chains, get_exposure_report


class GraphSearchState(TypedDict, total=False):
    query: str
    intent: dict[str, Any]
    linked_entities: list[dict[str, Any]]
    unresolved_entities: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    round: int
    decision: dict[str, Any]
    done: bool
    stop_reason: str
    errors: list[str]
    result: dict[str, Any]


def build_graph_search_workflow(
    store: OntologyStore, llm: Any, schema: OntologySchema
):
    max_rounds = int(os.environ.get("GRAPH_SEARCH_MAX_ROUNDS", "6"))
    max_rows = int(os.environ.get("GRAPH_SEARCH_ROW_LIMIT", "100"))
    linker = EntityLinkingService(store, llm=llm)

    def prepare(state: GraphSearchState) -> dict[str, Any]:
        query = state["query"]
        intent = _understand_query(llm, query, schema)
        mentions = [
            Mention(
                mention_id=str(item.get("mention_id") or f"q{index + 1}"),
                text=str(item.get("text") or "").strip(),
                normalized_text=normalize_surface(str(item.get("text") or "")),
                expected_types=[
                    str(value).lower() for value in item.get("expected_types") or []
                    if schema.is_entity_type(str(value))
                ],
                semantic_role=str(item.get("semantic_role") or ""),
                context=query,
                confidence=float(item.get("confidence", 0.8)),
            )
            for index, item in enumerate(intent.get("mentions") or [])
            if str(item.get("text") or "").strip()
        ]
        if not mentions:
            mentions = [Mention(text=query, context=query, confidence=0.5)]
        decisions = linker.link(mentions, mode="query", persist=False)
        linked = [decision.to_dict() for decision in decisions if decision.entity_id]
        unresolved = [decision.to_dict() for decision in decisions if not decision.entity_id]
        return {
            "intent": intent,
            "linked_entities": linked,
            "unresolved_entities": unresolved,
            "observations": [],
            "round": 0,
            "done": False,
            "stop_reason": "",
            "errors": [],
        }

    def decide(state: GraphSearchState) -> dict[str, Any]:
        if state.get("round", 0) >= max_rounds:
            return {
                "decision": {"action": "finish"},
                "done": True,
                "stop_reason": "maximum rounds reached",
            }
        decision = _next_decision(llm, state, schema, max_rows)
        if decision.get("action") == "finish":
            return {
                "decision": decision,
                "done": True,
                "stop_reason": str(decision.get("reason") or "agent reported sufficient evidence"),
            }
        fingerprint = json.dumps(
            [decision.get("cypher"), decision.get("parameters")],
            sort_keys=True,
            ensure_ascii=False,
        )
        if any(obs.get("fingerprint") == fingerprint for obs in state.get("observations", [])):
            return {
                "decision": decision,
                "done": True,
                "stop_reason": "repeated query detected",
            }
        return {"decision": decision, "done": False}

    def execute(state: GraphSearchState) -> dict[str, Any]:
        decision = state["decision"]
        started_round = state.get("round", 0) + 1
        fingerprint = json.dumps(
            [decision.get("cypher"), decision.get("parameters")],
            sort_keys=True,
            ensure_ascii=False,
        )
        observation: dict[str, Any] = {
            "round": started_round,
            "goal": decision.get("goal", ""),
            "cypher": decision.get("cypher", ""),
            "parameters": decision.get("parameters") or {},
            "fingerprint": fingerprint,
        }
        errors = list(state.get("errors", []))
        try:
            result = run_readonly_cypher(
                store,
                str(decision.get("cypher") or ""),
                dict(decision.get("parameters") or {}),
                max_rows=max_rows,
            )
            observation.update(result)
        except Exception as exc:
            observation["error"] = str(exc)
            errors.append(str(exc))
        return {
            "round": started_round,
            "observations": [*state.get("observations", []), observation],
            "errors": errors,
        }

    def finalize(state: GraphSearchState) -> dict[str, Any]:
        linked_ids = [
            item.get("entity_id") for item in state.get("linked_entities", [])
            if item.get("entity_id")
        ]
        entity_context = [
            inspect_entity(store, entity_id) for entity_id in linked_ids[:8]
        ]
        dependency_chains = []
        for entity_id in linked_ids[:8]:
            try:
                dependency_chains.extend(find_dependency_chains(
                    store,
                    entity_id,
                    max_depth=5,
                    rel_types=schema.dependency_rel_enums(),
                ))
            except Exception:
                pass
        try:
            exposure_scores = get_exposure_report(store, entity_ids=linked_ids)
        except Exception:
            exposure_scores = []
        try:
            degrees = degree_by_entity(store)
        except Exception:
            degrees = {}
        hubs = []
        for entity_id in linked_ids:
            entity = store.get_entity(entity_id)
            if not entity:
                continue
            hubs.append({
                "id": entity.id,
                "name": entity.name,
                "type": entity.entity_type.value,
                "degree": degrees.get(entity_id, 0),
                "description": entity.description[:100],
            })
        hubs.sort(key=lambda item: item["degree"], reverse=True)
        observations = state.get("observations", [])
        summary = str(state.get("decision", {}).get("summary") or "")
        findings = [summary] if summary else []
        findings.extend(
            f"Round {item['round']} returned {item.get('row_count', 0)} rows for "
            f"{item.get('goal') or 'graph evidence search'}."
            for item in observations if not item.get("error")
        )
        findings.extend(
            f"Graph search error: {item['error']}" for item in observations if item.get("error")
        )
        result = {
            "query": state["query"],
            "dependency_chains": dependency_chains,
            "exposure_scores": exposure_scores,
            "critical_paths": _extract_paths(observations),
            "hub_entities": hubs,
            "traversal_stats": {
                "total_entities": store.entity_count,
                "total_relationships": store.relationship_count,
                "focus_entities": len(linked_ids),
                "agent_rounds": state.get("round", 0),
                "rows_observed": sum(item.get("row_count", 0) for item in observations),
            },
            "key_findings": findings[:8] or ["No graph evidence was found."],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "linked_entities": state.get("linked_entities", []),
            "unresolved_entities": state.get("unresolved_entities", []),
            "search_trace": observations,
            "evidence": entity_context,
            "stop_reason": state.get("stop_reason", ""),
            "schema_version": "agentic-graph-search-v1",
        }
        return {"result": result}

    graph = StateGraph(GraphSearchState)
    graph.add_node("prepare", prepare)
    graph.add_node("decide", decide)
    graph.add_node("execute", execute)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "decide")
    graph.add_conditional_edges(
        "decide",
        lambda state: "finalize" if state.get("done") else "execute",
        {"finalize": "finalize", "execute": "execute"},
    )
    graph.add_conditional_edges(
        "execute",
        lambda state: "finalize" if state.get("round", 0) >= max_rounds else "decide",
        {"finalize": "finalize", "decide": "decide"},
    )
    graph.add_edge("finalize", END)
    return graph.compile()


def _understand_query(llm: Any, query: str, schema: OntologySchema) -> dict[str, Any]:
    raw = invoke_llm(
        llm,
        "Understand the graph intelligence question. Do not assign entity IDs. "
        "Return JSON only with question_type, mentions, relationship_hints, "
        "constraints and answer_goal.\n" + schema.analysis_prompt_block(),
        query,
    )
    payload = _json_object(raw)
    return payload or {
        "question_type": "graph_search",
        "mentions": [],
        "relationship_hints": [],
        "constraints": {"max_hops": 3},
        "answer_goal": query,
    }


def _next_decision(
    llm: Any,
    state: GraphSearchState,
    schema: OntologySchema,
    max_rows: int,
) -> dict[str, Any]:
    schema_info = describe_graph_schema(schema)
    raw = invoke_llm(
        llm,
        "You control a multi-step Neo4j evidence search. Based on prior observations, "
        "either return a parameterized read-only Cypher query or finish. "
        "Return JSON only. Query form: "
        '{"action":"query","goal":"","cypher":"","parameters":{}}. '
        'Finish form: {"action":"finish","reason":"","summary":""}. '
        "Cypher must use :Entity, schema relationship types, bounded paths <= 6, "
        "parameters for values, and LIMIT.\n"
        + json.dumps(schema_info, ensure_ascii=False),
        json.dumps({
            "query": state["query"],
            "intent": state.get("intent"),
            "linked_entities": state.get("linked_entities"),
            "unresolved_entities": state.get("unresolved_entities"),
            "observations": state.get("observations"),
        }, ensure_ascii=False, default=str),
    )
    decision = _json_object(raw)
    if decision.get("action") in ("query", "finish"):
        return decision
    if state.get("observations"):
        return {
            "action": "finish",
            "reason": "model returned no further valid search action",
            "summary": "Search completed from the available graph evidence.",
        }
    entity_ids = [
        item.get("entity_id") for item in state.get("linked_entities", [])
        if item.get("entity_id")
    ]
    return {
        "action": "query",
        "goal": "Explore bounded evidence paths around linked entities",
        "cypher": (
            "MATCH p=(a:Entity)-[*1..3]-(b:Entity) "
            "WHERE a.id IN $entity_ids "
            "RETURN p, length(p) AS hops ORDER BY hops LIMIT $limit"
        ),
        "parameters": {"entity_ids": entity_ids, "limit": max_rows},
    }


def _json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_paths(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths = []
    for observation in observations:
        for row in observation.get("rows") or []:
            path = row.get("p") or row.get("path")
            if not isinstance(path, dict) or "nodes" not in path:
                continue
            nodes = path["nodes"]
            paths.append({
                "source": nodes[0].get("id") if nodes else "",
                "source_name": nodes[0].get("name") if nodes else "",
                "target": nodes[-1].get("id") if nodes else "",
                "target_name": nodes[-1].get("name") if nodes else "",
                "path": [
                    {"id": node.get("id"), "name": node.get("name")}
                    for node in nodes
                ],
                "hops": row.get("hops", max(0, len(nodes) - 1)),
            })
    return paths[:20]
