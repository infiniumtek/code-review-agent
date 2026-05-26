# CI wrapper examples

Thin per-platform wrappers that run the **same** `code-review-agent` worker
container against a pull/merge request diff. Nothing platform-specific lives in
the agent — each CI integration is just the container plus the right env vars and
one selected reporter.

| File | Platform | Default reporter (`auto`) | Durable output |
| --- | --- | --- | --- |
| [`github-action/action.yml`](github-action/action.yml) | GitHub Actions | `github` + `terminal` | idempotent PR comment |
| [`gitlab-ci/.gitlab-ci.yml`](gitlab-ci/.gitlab-ci.yml) | GitLab CI | `gitlab` + `terminal` | idempotent MR note |
| [`jenkins/Jenkinsfile`](jenkins/Jenkinsfile) | Jenkins | `terminal` + `file` | archived artifact |

Jenkins has no native PR comments, so it relies on the `file` reporter + artifact
archiving. GitHub/GitLab additionally archive the `file` artifact in these
examples.

---

## The worker image

All three examples assume a published worker image built from this repo's
[`Dockerfile`](../Dockerfile):

```bash
docker build -t ghcr.io/your-org/code-review-agent:latest .
docker push   ghcr.io/your-org/code-review-agent:latest
```

Replace `ghcr.io/your-org/code-review-agent:latest` everywhere with your image.
The image bundles the **trusted** `skills/` and `review.toml` and ships `git`
(the CLI shells out to `git diff`/`git show`).

The entrypoint is the CLI:

```
code-review [RANGE] --repo PATH --reporter ... --fail-on ...
```

For a CI review the `RANGE` is `<base>...HEAD` (three-dot). A two-/three-dot range
makes the agent read new-side file content with `git show <head>:<path>` — correct
regardless of checkout state — and treats `<base>` as the diff base.

---

## The contract every example must honor

These are not optional knobs — they are the trust boundary (see
[`PLAN.md`](../PLAN.md) "Security & trust model" and `CLAUDE.md` §6/§13). Getting
one wrong silently degrades to default config or leaks PR-controlled input into
the reviewer prompt.

1. **Run with the container's working directory set to the checkout.**
   `config.py` reads the trusted `review.toml` with a bare `git show <ref>:review.toml`
   (no `-C`), so it discovers the repo from the **current directory**. If cwd is
   not inside the checkout the read fails and silently falls back to default
   config. GitHub: `docker run -w /workspace`. GitLab/Jenkins: the job already
   runs with cwd = the cloned project dir.

2. **Set `TRUSTED_CONFIG_REF` to the *base* ref**, never the PR/MR head.
   `review.toml` is read from `git show <base>:review.toml` — a PR cannot rewrite
   its own review rules (`fail_on`, ignore globs, enabled skills). With no trusted
   ref a CI run **fails closed** (`UntrustedConfigError`). The base sha must be in
   local history: GitHub `actions/checkout` with `fetch-depth: 0`, GitLab
   `GIT_DEPTH: "0"`, Jenkins `git fetch origin <target>`.

3. **Pin `SKILLS_PATH` and `REVIEW_CONFIG` to the bundled absolute paths**
   (`/app/skills`, `/app/review.toml`) via real env vars. The defaults are
   cwd-relative; since cwd is the PR checkout (rule 1), unpinned defaults would
   resolve `./skills` / `./review.toml` into PR-controlled content. The image
   already sets these; the examples re-assert them for clarity.

4. **Make a CI marker visible inside the container** (`CI`, `GITHUB_ACTIONS`,
   `GITLAB_CI`, or `JENKINS_URL`). It does two things: (a) the `.env` file is
   **not** loaded under CI, so a PR-supplied checkout `.env` can't set
   operator-only fields; (b) `--reporter auto` resolves to the platform reporter.
   Note: `docker run` does **not** inherit the runner's env, so GitHub's composite
   action passes `-e CI=true -e GITHUB_ACTIONS=true` explicitly.

5. **Repo-local extra skills stay off.** `ALLOW_REPO_SKILLS` is unset (false).
   Only flip it on (`--allow-repo-skills`) if you trust the reviewed repo's
   `review.toml [skills].extra_paths` — it is a prompt-injection vector in CI.

6. **Mount the checkout read-only and write artifacts elsewhere.** The agent only
   reads diffs and reports; the read-only mount enforces that. The `file` reporter
   writes to a separate writable `REPORT_DIR`.

7. **`git safe.directory`.** When the checkout is owned by a different uid than the
   container user, git refuses with "detected dubious ownership". The examples pass
   `GIT_CONFIG_COUNT=1 / GIT_CONFIG_KEY_0=safe.directory / GIT_CONFIG_VALUE_0=<path>`
   so every internal git call trusts the mounted checkout.

---

## Env vars by concern

| Concern | Vars | Notes |
| --- | --- | --- |
| LLM | `OPENAI_API_KEY` \| `ANTHROPIC_API_KEY` \| `GOOGLE_API_KEY`, `DEFAULT_LLM_PROVIDER`, `DEFAULT_LLM_MODEL` | one key must match the provider; store as a masked secret |
| Trust | `TRUSTED_CONFIG_REF` (base ref), `TRUSTED_CONFIG_PATH` (repo-relative, default `review.toml`), `SKILLS_PATH`, `REVIEW_CONFIG`, `ALLOW_REPO_SKILLS` | `TRUSTED_CONFIG_PATH` is fed to `git show` — never an absolute path |
| GitHub reporter | `GITHUB_TOKEN` (needs `pull-requests: write`), `GITHUB_REPOSITORY`, `GITHUB_REF`/`GITHUB_EVENT_PATH`, `GITHUB_API_URL` | PR number comes from the event payload or `refs/pull/N/merge` |
| GitLab reporter | `GITLAB_TOKEN` (project/personal token with `api`), `CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID`, `CI_API_V4_URL` | `CI_JOB_TOKEN` usually **cannot** post notes — use `GITLAB_TOKEN` |
| Output | `REPORT_DIR` (writable), `REPORTER` (or `--reporter`) | reporters are composable: `terminal,file,github,gitlab` |

## Exit codes / gating

The run exits non-zero when a finding meets `--fail-on` (default `high`), failing
the job — a quality gate. Set `--fail-on off` for advisory-only reviews that post a
comment/artifact but never block the pipeline. Reporter failures are non-fatal and
do not change the exit code.
