"""Compiled LangGraph agent.

Phase 1 scaffold: a minimal ``START → placeholder → END`` graph so that
``langgraph.json``, the local dev server, and package wiring are exercisable
from day one. The real pipeline (ingest → detect → ``Send`` fan-out → review →
aggregate → report) and the typed ``AgentState`` from ``utils/state.py`` land
in later phases — see ``PLAN.md``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel


class AgentState(BaseModel):
    """Placeholder graph state (replaced by ``utils/state.py`` in Phase 3)."""

    diff: str = ""


def _placeholder(state: AgentState) -> dict[str, str]:
    """No-op node; ingest/detect/review/aggregate/report arrive in later phases."""
    return {}


def build_agent() -> Any:
    """Compile the placeholder graph.

    Returns ``Any`` because LangGraph's compiled-graph type is untyped to us
    (its package is excluded from strict checking); replaced with the real
    pipeline builder in Phase 10.
    """
    graph = StateGraph(AgentState)
    graph.add_node("placeholder", _placeholder)
    graph.add_edge(START, "placeholder")
    graph.add_edge("placeholder", END)
    return graph.compile()


# Module-level compiled graph referenced by langgraph.json (`agent.py:agent`).
agent = build_agent()
