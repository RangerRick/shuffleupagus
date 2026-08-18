.PHONY: dev lint format test build

dev:
	uv sync --all-groups

lint:
	uv run ruff check src/ tests/
	uv run ty check src/ tests/

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

test:
	uv run pytest

build:
	uv build
