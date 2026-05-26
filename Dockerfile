# syntax=docker/dockerfile:1
#
# Platform-neutral review worker. Entrypoint = the `code-review` CLI; SCM/CI
# integration is just a runtime-selected reporter. The reviewed checkout is
# mounted at /workspace (the agent only reads it — never writes back).

FROM python:3.13-slim AS base

# uv for reproducible installs (exact versions from uv.lock). Pin in a real release.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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

# Non-root runtime user.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Bundled, trusted defaults (overridable at runtime).
ENV SKILLS_PATH=/app/skills \
    REVIEW_CONFIG=/app/review.toml

# Mount the checkout to review here.
WORKDIR /workspace

ENTRYPOINT ["code-review"]
CMD ["--help"]
