"""Diff parsing and new-side content resolution.

The parser consumes unified git diffs and returns typed :class:`ChangedFile`
objects for files that still exist on the new side. Added files keep their full
content in the hunk diff itself; modified and renamed files can be enriched with
full new-side text through a content resolver.
"""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol

import structlog

from code_review_agent.utils.state import ChangedFile, ChangeKind

log = structlog.get_logger(__name__)

DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "**/*.lock",
    "**/*.min.js",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
)
DEFAULT_MAX_CONTENT_BYTES = 1_000_000


class ContentResolver(Protocol):
    """Return full new-side text for a repository-relative path, if available."""

    def __call__(self, path: str) -> str | None: ...


def working_tree_resolver(
    repo_root: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> ContentResolver:
    """Resolve file content from the local working tree.

    The resolver is deliberately read-only and hardened for untrusted diff paths:
    it rejects absolute paths, ``..`` traversal, non-files, and oversized files.
    """

    root = Path(repo_root).resolve()

    def resolve(path: str) -> str | None:
        safe_path = normalize_repo_path(path)
        if safe_path is None:
            log.warning("content_path_rejected", path=path, reason="unsafe")
            return None

        candidate = (root / safe_path).resolve()
        if not candidate.is_relative_to(root):
            log.warning("content_path_rejected", path=path, reason="outside_repo")
            return None
        try:
            if not candidate.is_file():
                return None
            if candidate.stat().st_size > max_bytes:
                log.warning(
                    "content_path_skipped",
                    path=safe_path,
                    reason="oversized",
                    max_bytes=max_bytes,
                )
                return None
            return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("content_read_failed", path=safe_path, error=str(exc))
            return None

    return resolve


def git_show_resolver(
    head_ref: str,
    *,
    repo_root: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> ContentResolver:
    """Resolve file content via ``git show <head_ref>:<path>``.

    This is the resolver for CI/range reviews: it reads the reviewed commit
    directly from git, so it is independent of the current checkout state.
    """

    cwd = str(Path(repo_root).resolve()) if repo_root is not None else None

    def resolve(path: str) -> str | None:
        safe_path = normalize_repo_path(path)
        if safe_path is None:
            log.warning("content_path_rejected", path=path, reason="unsafe")
            return None
        spec = f"{head_ref}:{safe_path}"
        try:
            proc = subprocess.run(
                ["git", "show", spec],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError:
            log.warning("git_unavailable", ref=head_ref, path=safe_path)
            return None
        if proc.returncode != 0:
            log.warning(
                "git_show_content_unavailable",
                ref=head_ref,
                path=safe_path,
                stderr=proc.stderr.strip(),
            )
            return None
        if len(proc.stdout.encode("utf-8")) > max_bytes:
            log.warning(
                "content_path_skipped",
                path=safe_path,
                reason="oversized",
                max_bytes=max_bytes,
            )
            return None
        return proc.stdout

    return resolve


def parse_diff(
    diff_text: str,
    *,
    resolver: ContentResolver | None = None,
    ignore_globs: Iterable[str] | None = None,
) -> list[ChangedFile]:
    """Parse a unified diff into reviewable changed files.

    Deleted files are skipped because there is no new-side file to review.
    Ignore globs are the built-in defaults plus caller-provided patterns.
    """

    files: list[ChangedFile] = []
    effective_ignores = merge_ignore_globs(ignore_globs)

    for patch in _split_file_patches(diff_text):
        parsed = _parse_file_patch(patch)
        if parsed is None:
            continue
        path, kind, hunk_diff = parsed
        if kind == "deleted":
            continue
        if path_matches_globs(path, effective_ignores):
            continue

        new_content = None
        if kind in ("modified", "renamed") and resolver is not None:
            new_content = resolver(path)

        files.append(
            ChangedFile(
                path=path,
                kind=kind,
                diff=hunk_diff,
                new_content=new_content,
            )
        )

    return files


def merge_ignore_globs(ignore_globs: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return built-in ignore globs followed by caller globs, deduplicated."""

    seen: set[str] = set()
    merged: list[str] = []
    for pattern in [*DEFAULT_IGNORE_GLOBS, *(ignore_globs or ())]:
        normalized = pattern.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return tuple(merged)


def path_matches_globs(path: str, globs: Iterable[str]) -> bool:
    """Return whether ``path`` matches any git-style ignore glob."""

    safe_path = normalize_repo_path(path)
    if safe_path is None:
        return True

    candidates = {safe_path}
    parts = safe_path.split("/")
    candidates.update("/".join(parts[index:]) for index in range(1, len(parts)))

    for pattern in globs:
        normalized_pattern = pattern.replace("\\", "/").strip()
        if not normalized_pattern:
            continue
        pattern_candidates = [normalized_pattern]
        if normalized_pattern.startswith("**/"):
            pattern_candidates.append(normalized_pattern[3:])
        for candidate in candidates:
            if any(fnmatch.fnmatchcase(candidate, pat) for pat in pattern_candidates):
                return True
    return False


def normalize_repo_path(path: str) -> str | None:
    """Normalize a repo-relative POSIX path, rejecting traversal/absolute paths."""

    candidate = path.replace("\\", "/").strip()
    candidate = candidate.removeprefix("./")
    if not candidate or candidate.startswith("/") or "\x00" in candidate:
        return None
    parts = [part for part in candidate.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _split_file_patches(diff_text: str) -> list[list[str]]:
    lines = diff_text.splitlines(keepends=True)
    if not lines:
        return []

    if not any(line.startswith("diff --git ") for line in lines):
        return [lines]

    patches: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if current:
                patches.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        patches.append(current)
    return patches


def _parse_file_patch(lines: list[str]) -> tuple[str, ChangeKind, str] | None:
    old_path: str | None = None
    new_path: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    saw_new_file = False
    saw_deleted_file = False
    hunk_start: int | None = None

    for index, line in enumerate(lines):
        if line.startswith("@@"):
            hunk_start = index
            break
        if line.startswith("diff --git "):
            old_path, new_path = _parse_diff_git_paths(line)
        elif line.startswith("new file mode "):
            saw_new_file = True
        elif line.startswith("deleted file mode "):
            saw_deleted_file = True
        elif line.startswith("rename from "):
            rename_from = normalize_repo_path(line.removeprefix("rename from ").strip())
        elif line.startswith("rename to "):
            rename_to = normalize_repo_path(line.removeprefix("rename to ").strip())
        elif line.startswith("--- "):
            old_path = _parse_marker_path(line, "--- ")
        elif line.startswith("+++ "):
            new_path = _parse_marker_path(line, "+++ ")

    if saw_deleted_file or new_path is None:
        kind: ChangeKind = "deleted"
        path = old_path
    elif saw_new_file or old_path is None:
        kind = "added"
        path = new_path
    elif rename_from is not None or rename_to is not None or old_path != new_path:
        kind = "renamed"
        path = rename_to or new_path
    else:
        kind = "modified"
        path = new_path

    if path is None:
        return None

    safe_path = normalize_repo_path(path)
    if safe_path is None:
        log.warning("diff_path_rejected", path=path, reason="unsafe")
        return None

    hunk_diff = "".join(lines[hunk_start:]) if hunk_start is not None else ""
    return safe_path, kind, hunk_diff


def _parse_diff_git_paths(line: str) -> tuple[str | None, str | None]:
    payload = line.removeprefix("diff --git ").strip()
    try:
        parts = shlex.split(payload)
    except ValueError:
        parts = payload.split(maxsplit=1)
    if len(parts) < 2:
        return None, None
    return _strip_git_prefix(parts[0]), _strip_git_prefix(parts[1])


def _parse_marker_path(line: str, marker: Literal["--- ", "+++ "]) -> str | None:
    payload = line.removeprefix(marker).strip()
    if payload == "/dev/null":
        return None
    return _strip_git_prefix(_unquote_path(payload))


def _unquote_path(payload: str) -> str:
    payload = payload.split("\t", maxsplit=1)[0]
    try:
        parts = shlex.split(payload)
    except ValueError:
        return payload
    if len(parts) == 1:
        return parts[0]
    return payload


def _strip_git_prefix(path: str) -> str | None:
    normalized = path.strip()
    if normalized.startswith(("a/", "b/")):
        normalized = normalized[2:]
    return normalize_repo_path(normalized)
