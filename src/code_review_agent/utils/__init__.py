"""Internal building blocks for the review pipeline.

Holds the typed graph state (:mod:`state`) plus the diff/detection/prompt/node
helpers that later phases fill in. Everything that crosses a module boundary is
a Pydantic model from :mod:`code_review_agent.utils.state` — never a bare dict
(see ``CLAUDE.md`` §10).
"""
