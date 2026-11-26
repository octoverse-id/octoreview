.DEFAULT_GOAL := help
SHELL := /bin/bash

# `make check` stays toolchain-only (uv + Python 3.12 or later, SQLite; no Docker, no network).
# CI runs every job on 3.15, the interpreter the image ships.
# `make release-check` is the full gate and needs Docker for the PostgreSQL suite.

PG_CONTAINER ?= octoreview-test-postgres
PG_PORT ?= 55432
PG_URL ?= postgres://postgres:postgres@localhost:$(PG_PORT)/octoreview
PYTEST_SETTINGS := DJANGO_SETTINGS_MODULE=config.settings_pytest
# `make bench-postgres` gets its own container and port, so it can run beside test-postgres.
PG_BENCH_PORT ?= 55434
# The S3-compatible emulator for `make test-storage` (docs/storage.md, "Emulator").
S3_CONTAINER ?= octoreview-test-s3
S3_PORT ?= 59000
# The server image `make docker-build` builds and `make docker-smoke` tests.
IMAGE ?= octoreview:local
S3_IMAGE ?= rustfs/rustfs:1.0.6@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a

help: ## List available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## Install the project, dev dependencies and the s3 extra with uv
	@command -v uv >/dev/null || { echo "make install needs uv; install it: https://docs.astral.sh/uv/" >&2; exit 1; }
	uv sync --extra dev --extra s3

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply ruff formatting and safe fixes
	uv run ruff check --fix .
	uv run ruff format .

checks: ## Django system checks, migration drift, import layers, grep gates and the API contract
	$(PYTEST_SETTINGS) uv run python manage.py check
	$(PYTEST_SETTINGS) uv run python manage.py makemigrations --check --dry-run
	uv run lint-imports
	@./scripts/check-forbidden.sh
	@$(MAKE) --no-print-directory openapi-check

test: ## Run the test suite on SQLite
	uv run pytest

test-postgres: ## Run the test suite on PostgreSQL in Docker (needs Docker)
	@command -v docker >/dev/null || { echo "test-postgres needs Docker; install it or use make test" >&2; exit 1; }
	@./scripts/test-postgres.sh $(PG_CONTAINER) $(PG_PORT) $(PG_URL)

bench: ## Report-only latency bench on SQLite (docs/spec.md section 17; BENCH_ARGS=--help)
	$(PYTEST_SETTINGS) uv run python scripts/bench.py $(BENCH_ARGS)

bench-postgres: ## The latency bench on a throwaway PostgreSQL 17 container (needs Docker)
	@command -v docker >/dev/null || { echo "bench-postgres needs Docker; install it or use make bench" >&2; exit 1; }
	@$(PYTEST_SETTINGS) ./scripts/test-postgres.sh $(PG_CONTAINER)-bench $(PG_BENCH_PORT) postgres://postgres:postgres@localhost:$(PG_BENCH_PORT)/octoreview uv run python scripts/bench.py $(BENCH_ARGS)

test-storage: ## Run the S3 tests and check_storage against an S3-compatible emulator (needs Docker)
	@command -v docker >/dev/null || { echo "test-storage needs Docker; install it" >&2; exit 1; }
	@./scripts/test-storage.sh $(S3_CONTAINER) $(S3_PORT) $(S3_IMAGE)

docker-build: ## Build the server image (needs Docker)
	@command -v docker >/dev/null || { echo "docker-build needs Docker; install it" >&2; exit 1; }
	docker build -t $(IMAGE) .

docker-smoke: ## Smoke-test the image named by IMAGE (default octoreview:local; needs Docker)
	@./scripts/docker-smoke.sh $(IMAGE)

up: ## Start octoreview + PostgreSQL with media on the local backend, then copy .octoreview-dev.env out (needs Docker)
	docker compose up -d --build --wait
	@$(MAKE) --no-print-directory dev-env

up-s3: ## The same with media on the RustFS S3 emulator (compose profile s3; needs Docker)
	OCTOREVIEW_STORAGE_BACKEND=s3 docker compose --profile s3 up -d --build --wait
	@$(MAKE) --no-print-directory dev-env

down: ## Stop the compose stack (data stays; docker compose down -v drops it)
	docker compose --profile s3 down

dev-env: ## Copy .octoreview-dev.env (base URL, demo key, tenant, application) out of the stack again
	@docker compose cp octoreview:/var/lib/octoreview/dev/.octoreview-dev.env .octoreview-dev.env 2>/dev/null || \
		{ echo "make dev-env: the stack is not running; start it with make up" >&2; exit 1; }
	@echo "wrote .octoreview-dev.env (git-ignored; holds the demo key)"

# `make run` (README "Local development without Docker"): the server on 127.0.0.1 without
# Docker, on SQLite with media on the local backend. It turns DEBUG on and seeds an all-scope demo
# key, so it never binds 0.0.0.0. A default changes only on the make command line
# (`make run PORT=8001`, `make run DATABASE_URL=...`): the recipe runs under `env -u DATABASE_URL
# -u PORT`, so an exported .env.example or a PORT meant for another tool cannot redirect it. There
# is no global PORT or DATABASE_URL assignment, so every other target keeps the shell's values.
run_value = $(if $(filter command line,$(origin $(1))),$($(1)),$(2))
comma := ,
RUN_PORT = $(call run_value,PORT,8000)
RUN_DATABASE_URL = $(call run_value,DATABASE_URL,)
RUN_ENV = env -u DATABASE_URL -u PORT \
	DJANGO_DEBUG=$(call run_value,DJANGO_DEBUG,true) \
	OCTOREVIEW_STORAGE_BACKEND=$(call run_value,OCTOREVIEW_STORAGE_BACKEND,local) \
	OCTOREVIEW_STORAGE_LOCATION_ID=$(call run_value,OCTOREVIEW_STORAGE_LOCATION_ID,local-dev) \
	OCTOREVIEW_MEDIA_ROOT=$(call run_value,OCTOREVIEW_MEDIA_ROOT,./var/media) \
	OCTOREVIEW_PUBLIC_BASE_URL=$(call run_value,OCTOREVIEW_PUBLIC_BASE_URL,http://127.0.0.1:$(RUN_PORT)) \
	DJANGO_ALLOWED_HOSTS=$(call run_value,DJANGO_ALLOWED_HOSTS,127.0.0.1$(comma)localhost) \
	OCTOREVIEW_OUTBOX_TRANSPORT=$(call run_value,OCTOREVIEW_OUTBOX_TRANSPORT,logging) \
	$(if $(RUN_DATABASE_URL),DATABASE_URL='$(RUN_DATABASE_URL)')
# The banner names the database engine and host, never the user or password in DATABASE_URL.
RUN_BANNER = import os, urllib.parse as p; \
	u = p.urlsplit(os.environ.get("DATABASE_URL") or "sqlite:///db.sqlite3"); \
	db = "postgresql at " + u.netloc.rpartition("@")[2] + u.path if u.scheme.startswith("postgres") else u.scheme + " (" + u.path[1:] + ")"; \
	print("make run: database " + db + ", " + os.environ["OCTOREVIEW_PUBLIC_BASE_URL"] + ", storage " + os.environ["OCTOREVIEW_STORAGE_BACKEND"])
# SO_REUSEADDR as runserver sets it: a port in TIME_WAIT is free, one with a listener is not.
RUN_PORT_PROBE = import socket, sys; s = socket.socket(); \
	s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1])))

run: ## Serve on 127.0.0.1:8000 without Docker: SQLite, local media, demo key in .octoreview-dev.env (needs uv)
	@command -v uv >/dev/null || { echo "make run needs uv; install it: https://docs.astral.sh/uv/" >&2; exit 1; }
	@uv run python -c '$(RUN_PORT_PROBE)' $(RUN_PORT) 2>/dev/null || \
		{ echo "make run: port $(RUN_PORT) is taken; run make run PORT=<other port>" >&2; exit 1; }
	@$(RUN_ENV) uv run python -c '$(RUN_BANNER)'
	@$(RUN_ENV) uv run python manage.py migrate --noinput
	@$(RUN_ENV) uv run python manage.py seed --dev-env .octoreview-dev.env
	@$(RUN_ENV) uv run python manage.py runserver 127.0.0.1:$(RUN_PORT)

quickstart: ## Create a review with a photo against make up or make run (reads .octoreview-dev.env)
	@./scripts/quickstart.sh .octoreview-dev.env

compose-quickstart: ## The CI compose check: clean, second and s3 runs of the quickstart (drops the stack's volumes)
	@./scripts/compose-quickstart.sh

seed: ## Print a new demo key once with a curl, against your environment (needs DJANGO_DEBUG=true)
	uv run python manage.py seed

openapi-check: ## Check docs/openapi.yaml, errors.md, events.md and sdk-surface.yaml against the server
	$(PYTEST_SETTINGS) uv run python manage.py check_contract

sdk-surface: ## Regenerate docs/sdk-surface.md from docs/sdk-surface.yaml, then check the contract
	$(PYTEST_SETTINGS) uv run python manage.py check_contract --write

storage-check: ## Round-trip a probe object through the configured storage backend
	uv run python manage.py check_storage

security: ## Audit locked dependencies for known vulnerabilities (needs network)
	@./scripts/security-audit.sh

llms-check: ## Fail when llms.txt links a missing local path (and prove the check can fail)
	@./scripts/check-llms-txt.sh
	@./scripts/check-llms-txt-test.sh

actionlint: ## Lint GitHub Actions workflows (needs actionlint on PATH)
	@actionlint -color

version-check: ## Fail unless pyproject, openapi.yaml, settings and a CHANGELOG heading name one version
	@./scripts/version-check.sh

placeholder-check: ## Fail while any bootstrap CI placeholder job remains
	@if grep -rn 'octoreview:placeholder' .github/workflows; then \
		echo "placeholder-check: bootstrap placeholder jobs remain; replace them before a release" >&2; exit 1; \
	fi; echo "placeholder-check OK"

check: lint checks llms-check test ## Fast gate (toolchain only)

release-check: check test-postgres test-storage docker-build docker-smoke placeholder-check ## Full pre-release gate (fails while placeholders remain)

export-rulesets: ## Export the live rulesets into .github/rulesets/ (needs gh with repo admin)
	@./scripts/export-rulesets.sh

.PHONY: help install lint format checks test test-postgres test-storage docker-build docker-smoke \
	up up-s3 down dev-env run quickstart compose-quickstart seed version-check security llms-check actionlint \
	placeholder-check check release-check export-rulesets bench bench-postgres
