.PHONY: dev lint format test build

dev:
	uv sync --all-groups

lint:
	uv run ruff check src/
	uv run ty check src/

format:
	uv run ruff format src/
	uv run ruff check --fix src/

test:
	uv run pytest

build:
	uv build
