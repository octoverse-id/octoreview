#!/usr/bin/env bash
# Audit the locked runtime dependencies (including the storage extras) with pip-audit.
set -euo pipefail
cd "$(dirname "$0")/.."
requirements=$(mktemp "${TMPDIR:-/tmp}/octoreview-requirements.XXXXXX")
trap 'unlink "$requirements" 2>/dev/null || true' EXIT
uv export --frozen --no-hashes --no-emit-project --extra s3 --format requirements-txt >"$requirements"
uv run pip-audit --strict --requirement "$requirements"
