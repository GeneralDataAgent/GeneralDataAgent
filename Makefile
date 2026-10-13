.PHONY: sync test lint format check api

sync:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

check: lint test

api:
	uv run uvicorn generaldataagent.api.app:app --reload
