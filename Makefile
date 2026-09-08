.PHONY: help install hooks check fmt lint types imports determinism licenses test cov audit secrets mutation mutation-baseline mutation-baseline-from-runner mutation-retire-flaky wheel-smoke docs clean

help:
	@echo "install      install the project and dev dependencies"
	@echo "hooks        install the pre-commit hooks into .git/hooks"
	@echo "check        run every gate (CI also scans the full git history for secrets)"
	@echo "docs         validate the Mintlify documentation build and links"
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
	@echo "mutation-retire-flaky KEY=<key>  drop one flaky entry from the baseline (the only way one leaves)"
	@echo "wheel-smoke  build the wheel, install it outside the checkout, run the corpus from elsewhere"

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

# The runner's statuses are the truth (docs/spec/assurance.md): download the nightly's
# `mutation-results` artefact and rebuild the baseline from it, keys from a local run.
mutation-baseline-from-runner:
	@test -n "$(RESULTS)" || { echo "usage: make mutation-baseline-from-runner RESULTS=path/to/mutation-results.txt [SOURCE=<digest the run was over>]"; exit 2; }
	test -d mutants || { rm -rf mutants; uv run mutmut run; }
	uv run python scripts/mutation_gate.py baseline --from-results "$(RESULTS)" $(if $(SOURCE),--source $(SOURCE),)

# A flaky entry survives every regeneration (docs/spec/assurance.md, the mutation gate); this
# is the only way one leaves the baseline, and it is a deliberate, reviewable edit.
mutation-retire-flaky:
	@test -n "$(KEY)" || { echo "usage: make mutation-retire-flaky KEY=<function>:<digest>"; exit 2; }
	uv run python scripts/mutation_gate.py retire-flaky "$(KEY)"

# The same gate ci.yml's `wheel` job runs: the installed artefact, not the editable install
# (docs/spec/assurance.md, Releases). The environment and the working directory are outside
# the checkout, so an import that only resolves from the source tree fails here.
docs:
	cd docs && npx mint@latest validate && npx mint@latest broken-links

wheel-smoke:
	set -eu; \
	rm -rf /tmp/wheelenv /tmp/wheelsmoke; \
	uv build --wheel --no-build-isolation; \
	uv venv /tmp/wheelenv; \
	uv pip install --python /tmp/wheelenv dist/*.whl; \
	corpus="$(CURDIR)/corpus"; \
	expected="$$(find "$$corpus/scenarios" -name '*.yaml' | wc -l)"; \
	mkdir -p /tmp/wheelsmoke; cd /tmp/wheelsmoke; \
	/tmp/wheelenv/bin/ledgergate --version; \
	/tmp/wheelenv/bin/ledgergate run --corpus "$$corpus" --out result.json; \
	python3 -c "import json,sys; s=json.load(open('result.json'))['summary']; e=int(sys.argv[1]); assert s['pass']==s['scenarios']==e and s['fail']==s['error']==s['skipped']==0, s; print(f\"installed wheel: {s['pass']}/{s['scenarios']} scenarios pass\")" "$$expected"; \
	/tmp/wheelenv/bin/ledgergate report --conformance result.json --require L2

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage coverage.xml mutants
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
