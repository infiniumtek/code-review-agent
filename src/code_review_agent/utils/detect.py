"""Changed-file detection: repository path/content -> review skill key.

Phase 5 stays deliberately small: it classifies an already-ingested
``ChangedFile`` into the skill key later phases will resolve through the skills
registry. The detector is path-first (special CI/infra paths, then extensions)
with a shebang fallback for extensionless scripts.

``None`` is an explicit "not classified" outcome. Phase 6's missing-skill
hard-fail applies only after a file has been classified as a programming
language key by this static detector; the detect node may still classify
otherwise-unrecognized files through skill frontmatter-declared extensions.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Final

from code_review_agent.utils.diffing import normalize_repo_path
from code_review_agent.utils.state import ChangedFile, SkillKind

LANGUAGE_SKILL_KEYS: Final[frozenset[str]] = frozenset({"python", "javascript", "java"})
CI_SKILL_KEYS: Final[frozenset[str]] = frozenset(
    {"dockerfile", "github-actions", "gitlab-ci", "jenkins"}
)

EXTENSION_SKILL_KEYS: Final[dict[str, str]] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".java": "java",
}

SPECIAL_FILENAME_SKILL_KEYS: Final[dict[str, str]] = {
    "Dockerfile": "dockerfile",
    "Jenkinsfile": "jenkins",
}

SPECIAL_EXACT_PATH_SKILL_KEYS: Final[dict[str, str]] = {
    ".gitlab-ci.yml": "gitlab-ci",
}

SPECIAL_PREFIX_SKILL_KEYS: Final[dict[str, str]] = {
    ".github/workflows/": "github-actions",
}

_DOCKERFILE_BASENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:Dockerfile(?:\..+)?|.+\.Dockerfile)$"
)
_PYTHON_COMMAND_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:python(?:\d+(?:\.\d+)?)?|pypy(?:\d+(?:\.\d+)?)?)$"
)
SHEBANG_COMMAND_SKILL_KEYS: Final[dict[str, str]] = {
    "bun": "javascript",
    "deno": "javascript",
    "java": "java",
    "node": "javascript",
    "nodejs": "javascript",
    "ts-node": "javascript",
    "tsx": "javascript",
}


def detect_skill_key(file: ChangedFile) -> str | None:
    """Return the review skill key for ``file``, or ``None`` when unclassified."""

    path = normalize_repo_path(file.path)
    if path is None:
        return None

    special_key = _detect_special_path_skill_key(path)
    if special_key is not None:
        return special_key

    extension_key = EXTENSION_SKILL_KEYS.get(PurePosixPath(path).suffix.lower())
    if extension_key is not None:
        return extension_key

    return detect_shebang_skill_key(_first_new_side_line(file))


def detect_special_path_skill_key(path: str) -> str | None:
    """Detect CI/infra skill keys from special repository paths/filenames."""

    safe_path = normalize_repo_path(path)
    if safe_path is None:
        return None

    return _detect_special_path_skill_key(safe_path)


def _detect_special_path_skill_key(safe_path: str) -> str | None:
    basename = PurePosixPath(safe_path).name

    if _DOCKERFILE_BASENAME_RE.match(basename):
        return SPECIAL_FILENAME_SKILL_KEYS["Dockerfile"]

    filename_key = SPECIAL_FILENAME_SKILL_KEYS.get(basename)
    if filename_key is not None:
        return filename_key

    exact_path_key = SPECIAL_EXACT_PATH_SKILL_KEYS.get(safe_path)
    if exact_path_key is not None:
        return exact_path_key

    for prefix, skill_key in SPECIAL_PREFIX_SKILL_KEYS.items():
        if _is_direct_yaml_child(safe_path, prefix):
            return skill_key
    return None


def _is_direct_yaml_child(path: str, prefix: str) -> bool:
    if not path.startswith(prefix):
        return False
    remainder = path.removeprefix(prefix)
    if "/" in remainder:
        return False
    return PurePosixPath(remainder).suffix.lower() in {".yml", ".yaml"}


def detect_shebang_skill_key(first_line: str | None) -> str | None:
    """Detect a language skill from a script shebang line."""

    if first_line is None:
        return None

    command = _shebang_command(first_line)
    if command is None:
        return None
    if _PYTHON_COMMAND_RE.match(command):
        return "python"
    return SHEBANG_COMMAND_SKILL_KEYS.get(command)


def skill_key_kind(skill_key: str) -> SkillKind | None:
    """Return whether a known skill key is a language or optional CI target."""

    if skill_key in LANGUAGE_SKILL_KEYS:
        return "language"
    if skill_key in CI_SKILL_KEYS:
        return "ci"
    return None


def _first_new_side_line(file: ChangedFile) -> str | None:
    if file.new_content is not None:
        return _first_line(file.new_content)
    return _first_new_side_line_from_diff(file.diff)


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        return line
    return None


def _first_new_side_line_from_diff(diff: str) -> str | None:
    """Return line 1 from a unified hunk when the hunk contains it.

    For added files, the first hunk is normally ``+1`` and the first ``+`` line
    is the shebang when present. For modified files without full content, this
    only detects a shebang if a hunk beginning at new-side line 1 includes it as
    either context or an added line.
    """

    in_first_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_first_hunk = _hunk_new_start(line) == 1
            continue
        if not in_first_hunk:
            continue
        if line.startswith(("+", " ")):
            return line[1:]
    return None


def _hunk_new_start(header: str) -> int | None:
    match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
    if match is None:
        return None
    return int(match.group(1))


def _shebang_command(line: str) -> str | None:
    candidate = line.lstrip("\ufeff")
    if not candidate.startswith("#!"):
        return None

    payload = candidate[2:].strip()
    if not payload:
        return None

    try:
        tokens = shlex.split(payload)
    except ValueError:
        tokens = payload.split()
    if not tokens:
        return None

    command = PurePosixPath(tokens[0]).name.lower()
    if command != "env":
        return command

    return _env_shebang_command(tokens[1:])


def _env_shebang_command(args: list[str]) -> str | None:
    for arg in args:
        if arg.startswith("-"):
            continue
        if "=" in arg and not arg.startswith(("/", "./", "../")):
            continue
        return PurePosixPath(arg).name.lower()
    return None
