"""Phase 1 smoke test: the placeholder graph compiles and runs START → END."""

from code_review_agent.agent import agent


def test_agent_compiles_and_runs() -> None:
    result = agent.invoke({"diff": ""})
    assert result["diff"] == ""
