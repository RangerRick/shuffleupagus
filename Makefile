.PHONY: dev lint format test coverage build smoke

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

coverage:
	uv run pytest --cov-report=json:coverage.json
	bash scripts/check_per_module_coverage.sh coverage.json

build:
	uv build

smoke:
	bash scripts/smoke_test.sh
