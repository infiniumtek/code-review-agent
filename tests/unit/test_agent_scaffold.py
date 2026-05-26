"""Smoke test: the compiled graph runs with an empty diff."""

from code_review_agent.agent import agent


def test_agent_compiles_and_runs() -> None:
    result = agent.invoke({"diff": ""})
    assert result["diff"] == ""
