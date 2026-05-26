"""Unit tests for Phase 5 changed-file detection."""

from __future__ import annotations

import pytest

from code_review_agent.utils.detect import (
    CI_SKILL_KEYS,
    EXTENSION_SKILL_KEYS,
    LANGUAGE_SKILL_KEYS,
    SHEBANG_COMMAND_SKILL_KEYS,
    SPECIAL_EXACT_PATH_SKILL_KEYS,
    SPECIAL_FILENAME_SKILL_KEYS,
    SPECIAL_PREFIX_SKILL_KEYS,
    detect_shebang_skill_key,
    detect_skill_key,
    detect_special_path_skill_key,
    skill_key_kind,
)
from code_review_agent.utils.state import ChangedFile


def _file(path: str, *, diff: str = "", new_content: str | None = None) -> ChangedFile:
    return ChangedFile(path=path, kind="added", diff=diff, new_content=new_content)


def test_detection_maps_and_skill_kinds_stay_in_sync() -> None:
    language_keys = set(EXTENSION_SKILL_KEYS.values()) | set(SHEBANG_COMMAND_SKILL_KEYS.values())
    language_keys.add("python")  # Python shebangs are matched by version-tolerant regex.
    ci_keys = (
        set(SPECIAL_FILENAME_SKILL_KEYS.values())
        | set(SPECIAL_EXACT_PATH_SKILL_KEYS.values())
        | set(SPECIAL_PREFIX_SKILL_KEYS.values())
    )

    assert language_keys == LANGUAGE_SKILL_KEYS
    assert ci_keys == CI_SKILL_KEYS
    assert all(skill_key_kind(key) == "language" for key in language_keys)
    assert all(skill_key_kind(key) == "ci" for key in ci_keys)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", "python"),
        ("tools/window.PYW", "python"),
        ("web/index.js", "javascript"),
        ("web/component.jsx", "javascript"),
        ("web/component.ts", "javascript"),
        ("web/component.tsx", "javascript"),
        ("src/main/java/App.java", "java"),
    ],
)
def test_detects_language_by_extension(path: str, expected: str) -> None:
    assert detect_skill_key(_file(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Dockerfile", "dockerfile"),
        ("docker/service/Dockerfile", "dockerfile"),
        ("Dockerfile.dev", "dockerfile"),
        ("deploy/Dockerfile.prod", "dockerfile"),
        ("api.Dockerfile", "dockerfile"),
        ("docker/api.Dockerfile", "dockerfile"),
        ("Jenkinsfile", "jenkins"),
        ("ci/Jenkinsfile", "jenkins"),
        (".github/workflows/ci.yml", "github-actions"),
        (".github/workflows/release.yaml", "github-actions"),
        (".gitlab-ci.yml", "gitlab-ci"),
    ],
)
def test_detects_special_ci_paths(path: str, expected: str) -> None:
    assert detect_special_path_skill_key(path) == expected
    assert detect_skill_key(_file(path)) == expected


@pytest.mark.parametrize(
    "path",
    [
        "dockerfile",
        "Dockerfile.",
        "gitlab-ci.yml",
        "sub/.gitlab-ci.yml",
        ".github/workflows/check.py",
        ".github/workflows/scripts/build.yml",
    ],
)
def test_non_special_ci_paths_remain_unclassified_by_special_matcher(path: str) -> None:
    assert detect_special_path_skill_key(path) is None


def test_github_workflow_helper_uses_extension_detection() -> None:
    assert detect_skill_key(_file(".github/workflows/check.py")) == "python"
    assert detect_skill_key(_file(".github/workflows/scripts/build.py")) == "python"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#!/usr/bin/env python3", "python"),
        ("#!/usr/bin/python3.13 -I", "python"),
        ("#!/usr/bin/env pypy3.10", "python"),
        ("#!/usr/bin/env -S node --enable-source-maps", "javascript"),
        ("#!/usr/bin/env TS_NODE_PROJECT=tsconfig.json ts-node", "javascript"),
        ("#!/usr/bin/env java --source 21", "java"),
        ("#!/usr/bin/env bash", None),
        ("print('not a shebang')", None),
    ],
)
def test_detects_shebang_lines(line: str, expected: str | None) -> None:
    assert detect_shebang_skill_key(line) == expected


def test_detects_extensionless_shebang_from_new_content() -> None:
    file = _file("scripts/run", new_content="#!/usr/bin/env python3\nprint('hi')\n")

    assert detect_skill_key(file) == "python"


def test_empty_new_content_returns_none_for_extensionless_file() -> None:
    assert detect_skill_key(_file("scripts/run", new_content="")) is None


def test_detects_bom_prefixed_shebang() -> None:
    file = _file("scripts/run", new_content="\ufeff#!/usr/bin/env python3\n")

    assert detect_skill_key(file) == "python"


def test_detects_extensionless_shebang_from_added_diff() -> None:
    file = _file(
        "bin/tool",
        diff="""@@ -0,0 +1,2 @@
+#!/usr/bin/env node
+console.log("hi")
""",
    )

    assert detect_skill_key(file) == "javascript"


def test_detects_extensionless_shebang_from_first_line_context() -> None:
    file = _file(
        "bin/tool",
        diff="""@@ -1,3 +1,3 @@
 #!/usr/bin/env python3
 print("same")
-print("old")
+print("new")
""",
    )

    assert detect_skill_key(file) == "python"


def test_deep_modified_hunk_does_not_trigger_shebang_detection() -> None:
    file = _file(
        "bin/tool",
        diff="""@@ -40,3 +40,3 @@
 #!/usr/bin/env python3
 print("same")
-print("old")
+print("new")
""",
    )

    assert detect_skill_key(file) is None


def test_unknown_file_returns_none() -> None:
    assert detect_skill_key(_file("README.md", new_content="# heading\n")) is None
    assert detect_skill_key(_file("cmd/server.go", new_content="package main\n")) is None
    assert detect_skill_key(_file("../escape.py")) is None


@pytest.mark.parametrize(
    ("skill_key", "expected"),
    [("python", "language"), ("github-actions", "ci"), ("unknown", None)],
)
def test_skill_key_kind(skill_key: str, expected: str | None) -> None:
    assert skill_key_kind(skill_key) == expected
