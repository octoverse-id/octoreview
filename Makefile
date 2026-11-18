.DEFAULT_GOAL := help
SHELL := /bin/bash

# `make check` stays toolchain-only (uv + Python 3.12, SQLite; no Docker, no network).
# `make release-check` is the full gate and needs Docker for the PostgreSQL suite.

PG_CONTAINER ?= octoreview-test-postgres
PG_PORT ?= 55432
PG_URL ?= postgres://postgres:postgres@localhost:$(PG_PORT)/octoreview
PYTEST_SETTINGS := DJANGO_SETTINGS_MODULE=config.settings_pytest

help: ## List available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## Install the project and dev dependencies with uv
	uv sync --extra dev

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply ruff formatting and safe fixes
	uv run ruff check --fix .
	uv run ruff format .

checks: ## Django system checks, migration drift, import layers and grep gates
	$(PYTEST_SETTINGS) uv run python manage.py check
	$(PYTEST_SETTINGS) uv run python manage.py makemigrations --check --dry-run
	uv run lint-imports
	@./scripts/check-forbidden.sh

test: ## Run the test suite on SQLite
	uv run pytest

test-postgres: ## Run the test suite on PostgreSQL in Docker (needs Docker)
	@command -v docker >/dev/null || { echo "test-postgres needs Docker; install it or use make test" >&2; exit 1; }
	@./scripts/test-postgres.sh $(PG_CONTAINER) $(PG_PORT) $(PG_URL)

storage-check: ## Round-trip a probe object through the configured storage backend
	uv run python manage.py check_storage

security: ## Audit locked dependencies for known vulnerabilities (needs network)
	@./scripts/security-audit.sh

llms-check: ## Fail when llms.txt links a missing local path (and prove the check can fail)
	@./scripts/check-llms-txt.sh
	@./scripts/check-llms-txt-test.sh

actionlint: ## Lint GitHub Actions workflows (needs actionlint on PATH)
	@actionlint -color

placeholder-check: ## Fail while any bootstrap CI placeholder job remains
	@if grep -rn 'octoreview:placeholder' .github/workflows; then \
		echo "placeholder-check: bootstrap placeholder jobs remain; replace them before a release" >&2; exit 1; \
	fi; echo "placeholder-check OK"

check: lint checks llms-check test ## Fast gate (toolchain only)

release-check: check test-postgres placeholder-check ## Full pre-release gate (fails while placeholders remain)

export-rulesets: ## Export the live rulesets into .github/rulesets/ (needs gh with repo admin)
	@./scripts/export-rulesets.sh

.PHONY: help install lint format checks test test-postgres security llms-check actionlint \
	placeholder-check check release-check export-rulesets
