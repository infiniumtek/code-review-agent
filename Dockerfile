# syntax=docker/dockerfile:1
#
# Platform-neutral review worker. Entrypoint = the `code-review` CLI; SCM/CI
# integration is just a runtime-selected reporter. The reviewed checkout is
# mounted read-only at /workspace; file artifacts belong under /reports.

FROM python:3.13-slim AS base

# uv for reproducible installs (exact versions from uv.lock). Pin in a real release.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# git is a RUNTIME dependency, not just a build tool: the CLI shells out to
# `git diff`/`git show` (utils/diffing.py, cli.py) and config.py reads the
# trusted-ref review.toml via `git show <ref>:review.toml`. CI runners also clone
# into the job container with git. python:3.13-slim omits git, so install it —
# without it every CI/range review fails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1) Resolve dependencies first (cached layer) from lockfiles only.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Add the project source + bundle the TRUSTED skills/ and review.toml into the image.
COPY src ./src
COPY skills ./skills
COPY review.toml ./review.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root runtime user. /reports is the default file-reporter target and may
# be replaced by a host-visible bind mount at runtime.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /reports \
    && chown -R appuser:appuser /app /reports
USER appuser

# Bundled, trusted defaults (overridable at runtime). REVIEW_CONFIG is the
# FILESYSTEM fallback (local/non-CI reads); the CI trusted-ref read instead uses
# the repo-relative TRUSTED_CONFIG_PATH (default "review.toml") via `git show`,
# so this absolute path is never passed to git.
ENV SKILLS_PATH=/app/skills \
    REVIEW_CONFIG=/app/review.toml \
    REPORT_DIR=/reports

# Run from the trusted application directory, not from the PR-controlled
# checkout. Review mounted code explicitly with `--repo /workspace`.
WORKDIR /app

ENTRYPOINT ["code-review"]
CMD ["--help"]
