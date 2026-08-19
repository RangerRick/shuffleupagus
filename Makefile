.PHONY: dev lint format test coverage build smoke

dev:
	uv sync --all-groups

lint:
	uv run ruff check src/ tests/ scripts/
	uv run ty check src/ tests/ scripts/
	uv run pyright

format:
	uv run ruff format src/ tests/ scripts/
	uv run ruff check --fix src/ tests/ scripts/

test:
	uv run pytest

coverage:
	uv run pytest --cov-report=json:coverage.json
	uv run python3 scripts/check_per_module_coverage.py coverage.json

build:
	uv build

smoke:
	bash scripts/smoke_test.sh
