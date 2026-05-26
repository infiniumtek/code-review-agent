"""code-review-agent — LLM-first, multi-language code & CI/CD review agent.

Pipeline lives on LangGraph (``StateGraph`` + ``Send`` fan-out) and review
knowledge ships as portable SKILL.md packages. See ``PLAN.md`` for the build
phases; ``agent.py`` exposes the compiled graph as ``agent``.
"""

__version__ = "0.1.0"
