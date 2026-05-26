# code-review-agent — dev tasks. Python targets run via ./.venv/bin (never bare python).

.PHONY: venv install lock fmt lint type test review dev langgraph-build docker-build docker-up clean

UV := ./.venv/bin/uv

venv:
	python3.13 -m venv .venv
	./.venv/bin/pip install --upgrade pip uv

install: venv
	$(UV) sync --inexact --extra dev

lock:
	$(UV) lock

fmt:
	$(UV) run ruff format src tests

lint:
	$(UV) run ruff check src tests
	$(UV) lock --check

type:
	$(UV) run mypy src

test:
	$(UV) run pytest

# Review a diff locally. Usage: `make review` (HEAD vs working tree) or
# `git diff main...HEAD | ./.venv/bin/code-review --reporter terminal`.
review:
	git diff | $(UV) run code-review --reporter terminal

# Local LangGraph dev server (LangSmith Studio UI). Reads ./langgraph.json.
dev:
	$(UV) run langgraph dev

# Build a deployment image from ./langgraph.json via langgraph-cli.
langgraph-build:
	$(UV) run langgraph build -t code-review-agent:dev

docker-build:
	docker compose build

docker-up:
	docker compose up

clean:
	rm -rf .venv .mypy_cache .pytest_cache .ruff_cache dist build *.egg-info
