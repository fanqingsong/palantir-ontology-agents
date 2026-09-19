"""LangGraph workflow definition.

Defines the StateGraph: START -> coordinator -> osint -> graph_analyst
-> threat_assessor -> briefing_drafter -> END

OSINT writes first, then Graph Analyst reads the same store after outbox drain
so the run sees a consistent graph.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import StateGraph, START, END

from src.graph.state import AgentState
from src.graph.nodes import (
    coordinator_node,
    osint_node,
    graph_analyst_node,
    threat_assessor_node,
    briefing_drafter_node,
)


def build_workflow(llm: Any = None) -> StateGraph:
    """Build and compile the multi-agent workflow graph.

    Architecture:
        START -> coordinator -> osint_collector -> graph_analyst
              -> threat_assessor -> briefing_drafter -> END

    Args:
        llm: Optional chat model injected into every specialist agent.

    Returns:
        Compiled LangGraph StateGraph ready for execution.
    """
    graph = StateGraph(AgentState)

    graph.add_node("coordinator", partial(coordinator_node, llm=llm))
    graph.add_node("osint_collector", partial(osint_node, llm=llm))
    graph.add_node("graph_analyst", partial(graph_analyst_node, llm=llm))
    graph.add_node("threat_assessor", partial(threat_assessor_node, llm=llm))
    graph.add_node("briefing_drafter", partial(briefing_drafter_node, llm=llm))

    graph.add_edge(START, "coordinator")
    graph.add_edge("coordinator", "osint_collector")
    graph.add_edge("osint_collector", "graph_analyst")
    graph.add_edge("graph_analyst", "threat_assessor")
    graph.add_edge("threat_assessor", "briefing_drafter")
    graph.add_edge("briefing_drafter", END)

    return graph.compile()
