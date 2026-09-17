"""Orchestrator: coordinates the multi-agent workflow.

Initializes the LLM, builds the LangGraph workflow, loads the ontology,
and executes the full intelligence analysis pipeline.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from langchain_core.messages import HumanMessage


class Orchestrator:
    """Main orchestrator for the multi-agent ontology workflow.

    Manages the full pipeline: query intake -> coordinator -> parallel agents
    -> sequential agents -> briefing output.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """Initialize the orchestrator.

        Args:
            model_name: OpenAI-compatible model name. Falls back to OPENAI_MODEL, then gpt-4o-mini.
            api_key: API key. Falls back to OPENAI_API_KEY.
            base_url: Compatible API base URL. Falls back to OPENAI_BASE_URL or OPENAI_API_BASE.
        """
        self.model_name = model_name or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
        self._workflow = None
        self._llm = None

    @property
    def llm(self):
        """Lazily initialize the LLM."""
        if self._llm is None and self.api_key:
            try:
                from langchain_openai import ChatOpenAI
                kwargs: dict[str, Any] = {
                    "model": self.model_name,
                    "api_key": self.api_key,
                    "temperature": 0,
                    "max_tokens": 4096,
                }
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._llm = ChatOpenAI(**kwargs)
            except Exception:
                self._llm = None
        return self._llm

    @property
    def workflow(self):
        """Lazily build the workflow."""
        if self._workflow is None:
            from src.graph.workflow import build_workflow
            self._workflow = build_workflow()
        return self._workflow

    def run(self, query: str) -> dict[str, Any]:
        """Execute the full multi-agent workflow.

        Args:
            query: Natural language intelligence query.

        Returns:
            Final AgentState dict with all results.
        """
        initial_state = {
            "messages": [HumanMessage(content=query)],
            "query": query,
            "ontology_store_data": {},
            "osint_results": {},
            "graph_results": {},
            "threat_results": {},
            "briefing": {},
            "timeline": [],
            "status": "started",
            "error": None,
        }

        try:
            result = self.workflow.invoke(initial_state)
            return result
        except Exception as e:
            return {
                **initial_state,
                "status": "error",
                "error": str(e),
            }

    def run_stream(self, query: str):
        """Execute the workflow with streaming output.

        Yields state updates as each node completes.

        Args:
            query: Natural language intelligence query.

        Yields:
            Tuple of (node_name, state_update) for each completed node.
        """
        initial_state = {
            "messages": [HumanMessage(content=query)],
            "query": query,
            "ontology_store_data": {},
            "osint_results": {},
            "graph_results": {},
            "threat_results": {},
            "briefing": {},
            "timeline": [],
            "status": "started",
            "error": None,
        }

        try:
            for event in self.workflow.stream(initial_state):
                for node_name, state_update in event.items():
                    yield node_name, state_update
        except Exception as e:
            yield "error", {"error": str(e), "status": "error"}
