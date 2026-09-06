.PHONY: help install hooks check fmt lint types imports determinism licenses test cov audit secrets mutation mutation-baseline clean

help:
	@echo "install      install the project and dev dependencies"
	@echo "hooks        install the pre-commit hooks into .git/hooks"
	@echo "check        run every gate (CI also scans the full git history for secrets)"
	@echo "fmt          format the code"
	@echo "lint         ruff lint"
	@echo "types        mypy --strict"
	@echo "imports      import-linter architecture contracts"
	@echo "determinism  ledger core purity gate"
	@echo "licenses     per-file SPDX boundary gate"
	@echo "test         pytest, offline"
	@echo "cov          pytest with coverage gates"
	@echo "audit        dependency vulnerability scan"
	@echo "secrets      gitleaks scan of the working tree"
	@echo "mutation     mutmut over the core and the registry, then the ratchet against .mutation-baseline.json (minutes)"
	@echo "mutation-baseline  fresh mutation run, then rewrite the baseline (docs/spec/assurance.md)"

install:
	uv sync --all-groups

hooks:
	uv run pre-commit install

check: lint types imports determinism licenses cov audit secrets
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

licenses:
	uv run python scripts/check_licenses.py

test:
	uv run pytest

cov:
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=90

audit:
	uv run pip-audit

secrets:
	uv run pre-commit run gitleaks --all-files

mutation:
	rm -rf mutants
	uv run mutmut run
	uv run python scripts/mutation_gate.py gate

mutation-baseline:
	rm -rf mutants
	uv run mutmut run
	uv run python scripts/mutation_gate.py baseline

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage coverage.xml mutants
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
