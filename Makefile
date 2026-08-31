.PHONY: help install check fmt lint types imports determinism test cov audit clean

help:
	@echo "install      install the project and dev dependencies"
	@echo "check        run every gate (what CI runs)"
	@echo "fmt          format the code"
	@echo "lint         ruff lint"
	@echo "types        mypy --strict"
	@echo "imports      import-linter architecture contracts"
	@echo "determinism  ledger core purity gate"
	@echo "test         pytest, offline"
	@echo "cov          pytest with coverage gates"
	@echo "audit        dependency vulnerability scan"

install:
	uv sync --all-groups

check: lint types imports determinism cov
	@echo ""
	@echo "all gates passed"

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

types:
	uv run mypy

imports:
	uv run lint-imports

determinism:
	uv run python scripts/check_determinism.py

test:
	uv run pytest

cov:
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=90

audit:
	uv run pip-audit

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
