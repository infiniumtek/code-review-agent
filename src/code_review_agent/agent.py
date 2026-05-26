"""Compiled LangGraph agent.

Phase 6 wires ingest through skill-backed detection. Fan-out review,
aggregation, and reporting land in later phases — see ``PLAN.md``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from code_review_agent.utils.nodes import detect, ingest
from code_review_agent.utils.state import AgentState


def build_agent() -> Any:
    """Compile the current graph.

    Returns ``Any`` because LangGraph's compiled-graph type is untyped to us
    (its package is excluded from strict checking).
    """
    graph = StateGraph(AgentState)
    graph.add_node("ingest", ingest)
    graph.add_node("detect", detect)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "detect")
    graph.add_edge("detect", END)
    return graph.compile()


# Module-level compiled graph referenced by langgraph.json (`agent.py:agent`).
agent = build_agent()
