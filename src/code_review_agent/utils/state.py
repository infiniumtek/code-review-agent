"""Typed graph state for the review pipeline (Pydantic v2).

Every value that crosses a node boundary is one of the models here — no untyped
dicts (``CLAUDE.md`` §10). The shapes mirror the sketch in ``PLAN.md`` §State:

* :class:`ChangedFile` — one entry parsed from the diff (``ingest``).
* :class:`SkillRef` — a resolved skill (frontmatter only; body loaded lazily).
* :class:`ReviewUnit` — the files grouped under one skill (``detect``).
* :class:`Finding` — a single review remark (the atom of the report).
* :class:`ReviewResult` — the LLM's structured-output wrapper around findings.
* :class:`ReviewTaskState` — the **input schema of the ``review`` node** and the
  payload of each ``Send`` fan-out branch (carries exactly one unit).
* :class:`AgentState` — the overall graph state threaded START → END.

The fan-out reducer lives on the two ``findings`` fields: each ``review`` branch
returns ``{"findings": [...]}`` and the ``Annotated[list[Finding], add]`` reducer
concatenates the branches' lists into :attr:`AgentState.findings`. ``add`` is
``operator.add`` — list concatenation — which is what LangGraph applies when
merging concurrent node returns into a single state key.

Field ``description``s on :class:`Finding` / :class:`ReviewResult` are
intentional: those two models back ``with_structured_output`` (Phase 8), so the
descriptions become part of the schema the LLM is asked to fill.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "low", "medium", "high", "critical"]
Category = Literal["bug", "security", "performance", "improvement"]
ChangeKind = Literal["added", "modified", "renamed", "deleted"]
SkillKind = Literal["language", "ci"]


class ChangedFile(BaseModel):
    """One file touched by the diff.

    ``diff`` is the unified hunk text. ``new_content`` carries the full new-side
    text for **modified/renamed** files (attached by a ``ContentResolver`` in
    ``ingest``) so the reviewer can see context beyond the changed lines; it is
    ``None`` for added files (their full content is already in ``diff``), for
    deletes, and whenever no repo/ref is available (diff-only runs).
    """

    path: str
    kind: ChangeKind
    diff: str
    new_content: str | None = None


class SkillRef(BaseModel):
    """A resolved skill — Level-1 frontmatter plus where to find the body.

    Produced by the skills loader (Phase 6) from a ``SKILL.md`` package. Only the
    cheap frontmatter (``name``/``description``/``metadata``) is read up front;
    the SKILL.md **body** is loaded lazily from ``path`` when the unit is
    actually reviewed, so indexing every skill stays inexpensive.
    """

    key: str
    name: str
    description: str
    kind: SkillKind
    path: str


class ReviewUnit(BaseModel):
    """The files grouped under a single skill — one fan-out branch's workload."""

    skill: SkillRef
    files: list[ChangedFile]


class Finding(BaseModel):
    """A single review remark — the atom of the report.

    ``line`` is the new-side line number when the model can attribute one, else
    ``None`` (file-level finding). ``skill_key`` records which skill produced it
    so the report can group findings by reviewer.
    """

    path: str = Field(description="Repository-relative path of the file the finding refers to.")
    line: int | None = Field(
        default=None,
        description="New-side line number, or null for a file-level finding.",
    )
    severity: Severity = Field(description="Impact: info | low | medium | high | critical.")
    category: Category = Field(description="Kind: bug | security | performance | improvement.")
    title: str = Field(description="One-line summary of the issue.")
    detail: str = Field(description="Explanation and, where useful, a suggested fix.")
    skill_key: str = Field(description="Key of the skill that produced this finding.")


class ReviewResult(BaseModel):
    """Structured-output wrapper the LLM returns for one review unit.

    The model fills ``findings`` (possibly empty when the changes look clean);
    the ``review`` node unwraps it into ``{"findings": [...]}`` for the reducer.
    """

    findings: list[Finding] = Field(
        default_factory=list,
        description="All findings for the reviewed changes; empty if none.",
    )


class ReviewTaskState(BaseModel):
    """Input schema for the ``review`` node — one ``Send`` fan-out branch.

    The fan-out edge issues ``Send("review", ReviewTaskState(unit=u))`` per unit.
    The node reads :attr:`unit` and returns ``{"findings": [...]}``; the
    ``add`` reducer on :attr:`findings` makes the branch composable with the
    same-named key on :class:`AgentState` when LangGraph merges the returns.
    """

    unit: ReviewUnit
    findings: Annotated[list[Finding], add] = Field(default_factory=list)


class AgentState(BaseModel):
    """Overall graph state, threaded START → ingest → … → report → END.

    ``ingest`` populates :attr:`files`; ``detect`` populates :attr:`units`; the
    fan-out maps each unit to a ``review`` branch whose findings merge into
    :attr:`findings` via the ``add`` reducer; ``aggregate`` rewrites
    :attr:`findings` (dedupe + stable sort) and ``report`` fills :attr:`report`.

    :attr:`head_ref` is set for ``base...head`` runs and selects the
    ``git_show`` content resolver in ``ingest``; left ``None`` for local/piped
    diffs (working-tree resolver) and diff-only runs.
    """

    diff: str = ""
    repo_root: str | None = None
    head_ref: str | None = None
    files: list[ChangedFile] = Field(default_factory=list)
    units: list[ReviewUnit] = Field(default_factory=list)
    findings: Annotated[list[Finding], add] = Field(default_factory=list)
    report: str = ""
