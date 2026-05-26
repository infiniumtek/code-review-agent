"""Compiled LangGraph agent."""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from code_review_agent.utils.nodes import aggregate, detect, ingest, report, review
from code_review_agent.utils.state import AgentState, ReviewTaskState


def route_review_units(state: AgentState) -> list[Send] | str:
    """Fan out one review branch per unit, or skip straight to aggregation."""

    if not state.units:
        return "aggregate"
    return [
        Send(
            "review",
            ReviewTaskState(
                unit=unit,
                llm_provider_override=state.llm_provider_override,
                llm_model_override=state.llm_model_override,
            ),
        )
        for unit in state.units
    ]


def build_agent() -> Any:
    """Compile the review graph.

    Returns ``Any`` because LangGraph's compiled-graph type is untyped to us
    (its package is excluded from strict checking).
    """
    graph = StateGraph(AgentState)
    graph.add_node("ingest", ingest)
    graph.add_node("detect", detect)
    graph.add_node("review", cast(Any, review), input_schema=ReviewTaskState)
    graph.add_node("aggregate", aggregate)
    graph.add_node("report", report)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "detect")
    graph.add_conditional_edges("detect", route_review_units, ["review", "aggregate"])
    graph.add_edge("review", "aggregate")
    graph.add_edge("aggregate", "report")
    graph.add_edge("report", END)
    return graph.compile()


# Module-level compiled graph referenced by langgraph.json (`agent.py:agent`).
agent = build_agent()
