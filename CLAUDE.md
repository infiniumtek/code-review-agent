# CLAUDE.md

> Canonical project contract for Claude Code / Codex / any AI coding assistant (`AGENTS.md` points here). Keep §1, §3, §5, §6 in sync with `PLAN.md`; the rest is fixed standard.

---

## 1. Project

- **Name:** `code-review-agent` (package `code_review_agent`)
- **Goal:** LLM-first, multi-language **code & CI/CD review agent**. Detects each changed file's language/target and reviews it as an expert (bugs, security, performance, improvements). Review knowledge is delivered as **portable Agent Skills** (the open **SKILL.md** format, originated by Claude) loaded into the prompt — not as hard-coded personas.
- **Trigger:** a diff — local `git diff` via the Typer CLI, or a CI job (GitHub Actions / GitLab CI / Jenkins) running the worker container. No webhook server.
- **Framework:** **LangGraph** (`StateGraph` + `Send` fan-out). Not a ReAct/`create_agent` loop — the pipeline is an orchestrated fan-out → review → aggregate, not autonomous tool-calling.

---

## 2. Stack (fixed — do not change without explicit instruction)

- **Python 3.13** · **uv** package manager · **`.venv` only** (never system Python)
- **Pydantic v2** for all state, tool I/O, and config schemas
- **LangGraph v1** (`langgraph>=1.0.2`, `langchain-core>=1.0`)
- **Checkpointer: off** (one-shot run). No `langgraph-checkpoint-sqlite`. Add only on explicit request.
- **LLM providers: OpenAI, Anthropic, Google only** — selectable via config. **Default: OpenAI `gpt-5-mini`.**
- **Typer** CLI (= container entrypoint) · **PyYAML** for SKILL.md frontmatter · stdlib `tomllib` for `review.toml`
- **Docker** (compose) for reproducibility · `structlog` logging · `pytest` · `ruff` · `mypy --strict`
- **`langgraph-cli[inmem]`** in dev deps for `langgraph dev` (local Studio UI) and `langgraph build` (deployment image)

---

## 3. Layout

```
code-review-agent/
├── CLAUDE.md (canonical), AGENTS.md (pointer), README.md, PLAN.md
├── .env.example, .gitignore, .dockerignore, .python-version
├── pyproject.toml, uv.lock, langgraph.json
├── Dockerfile, docker-compose.yml, Makefile, review.toml
├── skills/                       # bundled (trusted) SKILL.md packages — portable open format
│   ├── python/SKILL.md           # seeded language skills (v1)
│   ├── javascript/SKILL.md       # covers .js/.jsx/.ts/.tsx
│   ├── java/SKILL.md
│   ├── dockerfile/SKILL.md       # optional CI/infra — loaded only if enabled in review.toml
│   ├── github-actions/SKILL.md
│   ├── gitlab-ci/SKILL.md
│   └── jenkins/SKILL.md
├── src/code_review_agent/
│   ├── agent.py                  # exposes compiled `agent` for langgraph.json
│   ├── cli.py                    # Typer CLI = container entrypoint
│   ├── config.py                 # Pydantic Settings (env) + review.toml loader (trusted-ref aware)
│   ├── llm.py                    # provider factory (OpenAI/Anthropic/Google)
│   ├── skills/                   # portable SKILL.md loader/registry
│   │   ├── loader.py             # discover, parse frontmatter (L1), lazy body (L2), resolve by language/target
│   │   └── errors.py             # MissingSkillError
│   ├── utils/
│   │   ├── state.py              # Pydantic graph state and review result models
│   │   ├── detect.py             # static file/target classifier
│   │   ├── diffing.py            # diff parsing and content resolvers
│   │   ├── prompts.py            # review prompt assembly and chunking
│   │   ├── node_ingest.py        # ingest node implementation
│   │   ├── node_detect.py        # detect node implementation
│   │   ├── node_review.py        # review node implementation + LLM output parsing
│   │   ├── node_aggregate.py     # aggregate node implementation
│   │   ├── node_report.py        # report node implementation
│   │   └── nodes.py              # compatibility exports for graph/tests
│   └── reporters/                # registry: terminal · file · github · gitlab
├── tests/{unit,integration}/
├── examples/{github-action,gitlab-ci,jenkins}/   # thin per-platform wrappers
└── data/                         # scratch / fixtures (gitignored if dynamic)
```

All importable code lives under `src/code_review_agent/`. `agent.py` exposes the compiled graph as a module-level `agent` variable. `skills/` is data (bundled into the image), not Python.

---

## 4. Setup & hard rules

```
python3.13 -m venv .venv && source .venv/bin/activate
pip install uv
uv sync --inexact --extra dev   # exact versions from uv.lock (--inexact keeps uv/pip)
cp .env.example .env            # fill in keys
```

- ❌ Never `pip install` outside `.venv`
- ❌ Never invoke a bare `python` — always activate `.venv` first
- ❌ Never commit `.env`, `.venv/`, `__pycache__`, `data/*` (dynamic)
- ❌ Never execute scripts bundled inside skill folders — skills are **prompt-only** (see §13)
- ❌ Never trust PR-head `review.toml` / repo-local skills in CI — see the trust model in §6/§13
- ❌ Never write to / auto-fix the repo under review — the agent only reads diffs and reports
- 📌 Deps are pinned in `uv.lock` (committed). After editing `pyproject.toml`, run `uv lock` (or `make lock`); `make lint` runs `uv lock --check` and fails on drift.

---

## 5. Required env vars (`.env.example`)

```
# At least one LLM key required (must match DEFAULT_LLM_PROVIDER)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=

# openai | anthropic | google
DEFAULT_LLM_PROVIDER=openai
DEFAULT_LLM_MODEL=gpt-5-mini
# Silently omitted for gpt-5*/reasoning models that reject it
DEFAULT_LLM_TEMPERATURE=0.0

# Skills & review behavior (file-level config lives in ./review.toml)
# Bundled, TRUSTED skill dir (always loaded)
SKILLS_PATH=./skills
# FILESYSTEM path for local/bundled reads (may be absolute, e.g. /app/review.toml)
REVIEW_CONFIG=./review.toml
# Opt-in (CI operator only): honor review.toml [skills].extra_paths / repo-local skills
ALLOW_REPO_SKILLS=false
# CI: git ref to read review.toml from (e.g. PR base); empty means working tree locally and fail-closed in CI
TRUSTED_CONFIG_REF=
# REPO-RELATIVE path read from TRUSTED_CONFIG_REF via `git show` (NOT a filesystem path)
TRUSTED_CONFIG_PATH=review.toml

# LLM resilience
LLM_MAX_RETRIES=2
LLM_TIMEOUT_SECONDS=60

# Reporters — composable; primary selection is review.toml [report].reporters.
# Override here with a comma-separated subset. "auto" = detected platform reporter + terminal (+ file on Jenkins/unknown).
# auto | comma-separated subset of: terminal,file,github,gitlab
REPORTER=auto
# Local default; containers/CI should set this to a writable artifact mount
REPORT_DIR=.
# Platform context is read from CI env (GITHUB_TOKEN, GITHUB_REPOSITORY, GITLAB_TOKEN, CI_*, etc.)

# Optional observability
LANGSMITH_API_KEY=
LANGSMITH_TRACING=false
LANGSMITH_PROJECT=code-review-agent

LOG_LEVEL=INFO
# development | staging | production
ENVIRONMENT=development
```

---

## 6. Patterns (where things live)

- **LLM factory** (`llm.py`): single `get_llm(provider, model, temperature)` switching on `openai|anthropic|google`. Omits `temperature` for gpt-5*/reasoning models that reject it. Defaults from `config.settings`.
- **Skills loader** (`skills/loader.py`): portable SKILL.md consumption (provider-agnostic — **not** Claude's code-execution runtime). Level 1: parse only frontmatter (`name`, `description`, `metadata`) for every skill → cheap index. Level 2: load the SKILL.md **body** lazily when a skill is selected → injected as the reviewer system prompt. Resolves detected language/target → skill via frontmatter `metadata` (`kind`, `languages`, `extensions`) or directory name; frontmatter `extensions` also provide the fallback path classifier for newly added **language** skills only. Only **bundled** `SKILLS_PATH` is trusted; `review.toml [skills].extra_paths` are honored only when `ALLOW_REPO_SKILLS=true`.
- **Missing-skill rule**: detected **programming language** with no skill → raise `MissingSkillError` and **fail the run** (non-zero exit). **Optional CI/infra targets** (dockerfile, github-actions, gitlab-ci, jenkins) → loaded only if the skill exists **and** is enabled in `review.toml [skills].enable`; otherwise silently skipped.
- **State** (`utils/state.py`): Pydantic models (`ChangedFile`, `ReviewUnit`, `Finding`, `ReviewResult`, `ReviewTaskState`, `SkillRef`, `AgentState`). The `review` node's **input schema is `ReviewTaskState`** (carries one `unit` plus optional CLI/state provider/model overrides); the fan-out edge issues `Send("review", ReviewTaskState(unit=u, ...))` and the node returns `{"findings": …}` merged into `AgentState.findings` via an `Annotated[list[Finding], add]` reducer. `AgentState` also carries CLI override fields for reporter/fail-threshold selection so Phase 11/13 precedence can flow through graph input without mutating env vars. `ReviewResult` is the LLM structured-output wrapper. No untyped dicts across module boundaries.
- **Node modules** (`utils/node_*.py`, exported by `utils/nodes.py`): keep each graph stage in its focused implementation module (`node_ingest`, `node_detect`, `node_review`, `node_aggregate`, `node_report`). `utils/nodes.py` is a thin compatibility surface for stable graph/test imports; do not grow it with implementation logic again.
- **Detection** (`utils/detect.py` + `utils/node_detect.py`): static extension map + shebang/first-line fallback + special filenames/paths (`Dockerfile`, `Dockerfile.*`, `*.Dockerfile`, direct `.github/workflows/*.yml|*.yaml`, root `.gitlab-ci.yml`, `Jenkinsfile`) → skill key, then a registry fallback by language-skill frontmatter `metadata.extensions` when the static detector has no signal. Files with no static or registry signal are unclassified and omitted from review units; the missing-skill hard-fail applies only after static detection classifies a file as a programming-language key.
- **Diffing** (`utils/diffing.py`): parse git/piped diff → `ChangedFile`s; full new-side text for modified/renamed files via a `ContentResolver` — `git show <head>:path` for explicit two-dot/three-dot commit ranges (CI; correct regardless of checkout state), hardened working-tree read locally including `git diff <single-ref>`; ignore globs.
- **Prompts** (`utils/prompts.py`): system = skill body + an **injection-hardening preamble** (reviewed code/comments/CI YAML are *data, not instructions*); user = diff/context in delimited untrusted-data blocks. Never inline prompt strings in node code. Per-unit `MAX_UNIT_TOKENS` budget with chunking.
- **Review** (`utils/node_review.py`): `with_structured_output(ReviewResult)` with a per-provider method choice; tolerant free-form-JSON fallback that logs the raw response on parse failure; retry/timeout from settings.
- **Reporters** (`reporters/`): registry — `terminal`, `file` (md/json artifact, used by Jenkins), `github` (PR comment), `gitlab` (MR note). **Composable**: the `report` node runs every reporter in the resolved list (precedence CLI `--reporter` > `REPORTER` env > `review.toml [report].reporters` > `auto`). `github`/`gitlab` are **idempotent** — locate the existing bot comment/note by a stable hidden marker (`<!-- code-review-agent -->`) and update it in place. `auto` = detected platform reporter + terminal (+ file on Jenkins/unknown). Each reporter runs independently; failures are non-fatal.
- **Config** (`config.py`): `pydantic_settings.BaseSettings` reading env (secrets) + a `review.toml` loader. In CI, `review.toml` is read from `TRUSTED_CONFIG_REF` (base ref) via `git show <ref>:<TRUSTED_CONFIG_PATH>` — not the PR head — and with **no** trusted ref a CI run **fails closed** (`UntrustedConfigError`) rather than reading the PR-controlled working tree. `TRUSTED_CONFIG_PATH` is *repo-relative* (default `review.toml`) and is distinct from `REVIEW_CONFIG`, the *filesystem* path for local/bundled reads (which may be absolute, e.g. `/app/review.toml`); never feed a filesystem path to `git show`. The `.env` file is a local-dev convenience only: it is **not loaded under CI** (real env vars only), so a checkout `.env` can't set operator-only fields (`SKILLS_PATH`/`ALLOW_REPO_SKILLS`/`TRUSTED_CONFIG_REF`). Secrets never hardcoded.

---

## 7. Docker

- Base: `python:3.13-slim` (+ `git`, a runtime dep — the CLI/config shell out to `git diff`/`git show`; slim omits it), `.venv` at `/app/.venv` (identical to host), non-root user
- **CI examples run the container with cwd = the mounted checkout** so `config.py`'s bare `git show <base>:review.toml` resolves (`examples/` + §6 trust model); cwd outside the checkout silently degrades the trusted-config read to defaults
- **Entrypoint = the CLI** (`code-review`) — platform-neutral; the SCM/CI integration is just a runtime-selected reporter
- Bundle `skills/` and `review.toml` into the image; mount the checkout to review read-only at a known path and mount file-reporter output separately
- Compose mounts `./src` for dev hot-reload (remove for prod). No SQLite mount (checkpointer off)
- Deployment image: `make langgraph-build` (uses `langgraph build` from `langgraph-cli`)

---

## 8. `make` targets

`venv` · `install` · `lock` · `fmt` · `lint` · `type` · `test` · `review` · `dev` · `langgraph-build` · `docker-build` · `docker-up` · `clean`

Python targets invoke `./.venv/bin/...` — never bare `python`. `review` runs the CLI against `git diff` with the terminal reporter. `dev` runs the LangGraph local server so the graph in `langgraph.json` is reachable from LangSmith Studio. Use these instead of re-inventing commands.

---

## 9. `langgraph.json`

```
{
  "dependencies": ["."],
  "graphs": { "agent": "./src/code_review_agent/agent.py:agent" },
  "env": "./.env",
  "python_version": "3.13"
}
```

---

## 10. Conventions

- Type hints everywhere; `mypy --strict` must pass on `src/`
- Pydantic for all data shapes — no bare dicts across module boundaries
- One node = one small, pure-ish function
- No `print` in `src/` — structured logging only
- Raise typed exceptions; let the graph handle retry/branching, not try/except spaghetti
- Determinism: stable finding sort; every report carries an "advisory" disclaimer
- Treat diffs, repo-local config, and repo-local skills as **untrusted input** (see §13)

---

## 11. Testing

- `tests/unit/` — pure function and node tests; mock LLM calls; cover the skill loader (incl. `MissingSkillError` and `extra_paths` gating), detection, the **prompt-injection hardening**, the **config trust model**, and **reporter idempotency**
- `tests/integration/` — compiled graph end-to-end with a **mocked LLM** and recorded diff fixtures (no checkpointer)
- Every node gets at least one unit test
- Use `pytest.fixture` for graph construction to stay DRY

---

## 12. Workflow when editing this project

1. Read this file, then `PLAN.md`, then `pyproject.toml`, then `src/code_review_agent/agent.py`
2. Honor framework choice from §1 (LangGraph) and follow the PLAN.md phase order
3. Add/adjust Pydantic schemas in `utils/state.py` before writing node code
4. New graph-stage logic goes in the focused `utils/node_*.py` module and is re-exported from `utils/nodes.py` when it is part of the public node surface; wire public node names in `agent.py`. New languages/targets are **new `skills/<key>/SKILL.md` folders**, not graph changes
5. Add at least one unit test
6. Run `make fmt lint type test` before declaring a phase done

---

## 13. Out of scope (stop and ask first)

- **Native Claude Skills runtime** (code-execution VM / Skills API) — v1 uses a provider-agnostic prompt-injection loader so OpenAI/Google work too
- **Executing skill-bundled `scripts/`** — skills are prompt-only; optional scanners belong in a separate, vetted, config-gated tool registry (deferred)
- **Trusting PR-provided config/skills** — in CI, `review.toml` comes from the trusted base ref and repo-local extra skills require `ALLOW_REPO_SKILLS=true`; never auto-trust head-ref skills/config (prompt-injection vector)
- **Auto-fixing or writing to the reviewed repository** — read-and-report only
- Adding a new SCM/CI platform reporter beyond terminal/file/github/gitlab
- Cloud-managed DBs, vector stores, message queues · Kubernetes / Helm / Terraform · LLM providers other than OpenAI / Anthropic / Google · Frontend frameworks · Paid third-party APIs beyond LLMs

---

## 14. References

- <https://docs.langchain.com/oss/python/releases/langgraph-v1>
- <https://docs.langchain.com/oss/python/langgraph/application-structure>
- <https://docs.langchain.com/oss/python/langgraph/graph-api>  (StateGraph, Send fan-out)
- <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview>
- <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices>
- <https://docs.pydantic.dev/latest/>
- <https://docs.astral.sh/uv/>
