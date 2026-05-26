"""Unit tests for Phase 4 diff ingest and content resolvers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from code_review_agent.config import ReviewConfig
from code_review_agent.utils.diffing import (
    git_show_resolver,
    parse_diff,
    path_matches_globs,
    working_tree_resolver,
)
from code_review_agent.utils.nodes import ingest_files
from code_review_agent.utils.state import AgentState


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")


def test_parse_diff_added_and_modified_files() -> None:
    diff = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+print("hi")
+value = 1
diff --git a/existing.py b/existing.py
index 2222222..3333333 100644
--- a/existing.py
+++ b/existing.py
@@ -1 +1 @@
-old
+new
"""
    resolved: list[str] = []

    def resolver(path: str) -> str | None:
        resolved.append(path)
        return f"full content for {path}"

    files = parse_diff(diff, resolver=resolver)

    assert [file.path for file in files] == ["new.py", "existing.py"]
    assert files[0].kind == "added"
    assert files[0].new_content is None
    assert '+print("hi")' in files[0].diff
    assert files[1].kind == "modified"
    assert files[1].new_content == "full content for existing.py"
    assert resolved == ["existing.py"]


def test_parse_diff_renamed_file_uses_new_path_for_content() -> None:
    diff = """diff --git a/old.py b/pkg/new.py
similarity index 87%
rename from old.py
rename to pkg/new.py
index 1111111..2222222 100644
--- a/old.py
+++ b/pkg/new.py
@@ -1 +1 @@
-old_name()
+new_name()
"""

    files = parse_diff(diff, resolver=lambda path: f"content:{path}")

    assert len(files) == 1
    assert files[0].path == "pkg/new.py"
    assert files[0].kind == "renamed"
    assert files[0].new_content == "content:pkg/new.py"


def test_parse_diff_skips_deleted_files() -> None:
    diff = """diff --git a/remove.py b/remove.py
deleted file mode 100644
index 1111111..0000000
--- a/remove.py
+++ /dev/null
@@ -1 +0,0 @@
-gone()
"""

    assert parse_diff(diff) == []


def test_parse_diff_applies_default_and_config_ignore_globs() -> None:
    diff = """diff --git a/uv.lock b/uv.lock
index 1111111..2222222 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1 +1 @@
-old
+new
diff --git a/docs/generated.py b/docs/generated.py
index 1111111..2222222 100644
--- a/docs/generated.py
+++ b/docs/generated.py
@@ -1 +1 @@
-old
+new
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+new
"""

    files = parse_diff(diff, ignore_globs=["docs/**"])

    assert [file.path for file in files] == ["src/app.py"]


def test_glob_matching_handles_root_and_nested_defaults() -> None:
    assert path_matches_globs("uv.lock", ["**/*.lock"])
    assert path_matches_globs("src/vendor/pkg/a.py", ["**/vendor/**"])
    assert path_matches_globs("node_modules/pkg/index.js", ["**/node_modules/**"])


def test_working_tree_resolver_hardens_paths_and_skips_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "safe.py").write_text("print('safe')\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "big.py").write_text("too large", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")

    resolver = working_tree_resolver(tmp_path, max_bytes=4)

    assert resolver("safe.py") is None  # skipped because the test max is intentionally tiny
    assert resolver("../outside.py") is None
    assert resolver(str(outside)) is None
    assert resolver("pkg") is None
    assert resolver("big.py") is None

    normal_resolver = working_tree_resolver(tmp_path)
    assert normal_resolver("safe.py") == "print('safe')\n"


def test_git_show_resolver_reads_reviewed_commit_not_current_checkout(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_ref = _git(tmp_path, "rev-parse", "HEAD")

    target.write_text("head\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "head", "-q")
    head_ref = _git(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "--detach", base_ref, "-q")

    assert working_tree_resolver(tmp_path)("a.py") == "base\n"
    assert git_show_resolver(head_ref, repo_root=tmp_path)("a.py") == "head\n"


def test_ingest_uses_git_show_when_head_ref_is_set(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_ref = _git(tmp_path, "rev-parse", "HEAD")

    target.write_text("reviewed\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "head", "-q")
    head_ref = _git(tmp_path, "rev-parse", "HEAD")
    diff = _git(tmp_path, "diff", f"{base_ref}...{head_ref}")

    _git(tmp_path, "checkout", "--detach", base_ref, "-q")
    state = AgentState(diff=diff, repo_root=str(tmp_path), head_ref=head_ref)

    files = ingest_files(state, review_config=ReviewConfig())

    assert len(files) == 1
    assert files[0].path == "app.py"
    assert files[0].new_content == "reviewed\n"


def test_ingest_without_repo_root_is_diff_only() -> None:
    diff = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""
    state = AgentState(diff=diff, head_ref="HEAD")

    files = ingest_files(state, review_config=ReviewConfig())

    assert len(files) == 1
    assert files[0].new_content is None
