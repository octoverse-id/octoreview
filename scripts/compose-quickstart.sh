#!/usr/bin/env bash
# The `compose-quickstart` check (CI; `make compose-quickstart`; docs/spec.md section 19).
# Three runs, each `docker compose up` through the curl quickstart within the budget:
#   1. a clean stack on the local backend;
#   2. a second run against the same volumes (the seed is idempotent, the data persists);
#   3. a clean stack on the `s3` profile (RustFS).
# The image build is timed separately. Results go to $GITHUB_STEP_SUMMARY when set. The stack is
# torn down with its volumes at the end, so do not point it at a stack you want to keep.
set -euo pipefail
cd "$(dirname "$0")/.."
budget=${QUICKSTART_BUDGET_SECONDS:-300}
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
env_file=$(mktemp)

down() { docker compose --profile s3 down "$@" >/dev/null 2>&1 || true; }
finish() {
  status=$?
  if [ "$status" -ne 0 ]; then docker compose --profile s3 logs --no-color --tail 200 >&2 || true; fi
  down -v
  rm -f "$env_file"
  exit "$status"
}
trap finish EXIT

run() {
  local name=$1
  shift
  local started
  started=$(date +%s)
  "$@"
  docker compose cp octoreview:/var/lib/octoreview/dev/.octoreview-dev.env "$env_file" >/dev/null 2>&1
  scripts/quickstart.sh "$env_file"
  # The deployment smoke (docs/operations.md) against the same stack, cleaning up after itself.
  OCTOREVIEW_SMOKE_APPLICATION_ID=storefront scripts/smoke-upload.sh "$env_file"
  local took=$(($(date +%s) - started))
  echo "compose-quickstart: $name took ${took}s"
  echo "| $name | ${took} s |" >>"$summary"
  if [ "$took" -gt "$budget" ]; then
    echo "compose-quickstart: $name exceeded the ${budget}s budget" >&2
    exit 1
  fi
}

down -v
started=$(date +%s)
docker compose build --quiet
build=$(($(date +%s) - started))
echo "compose-quickstart: image build took ${build}s (not part of the budget)"
{
  echo "### compose-quickstart"
  echo
  echo "Image build: ${build} s (excluded). Budget per run: ${budget} s."
  echo
  echo "| Run | up to quickstart |"
  echo "| --- | --- |"
} >>"$summary"

run "local backend, clean" docker compose up -d --wait
down
run "local backend, second run" docker compose up -d --wait
down -v
run "s3 profile, clean" env OCTOREVIEW_STORAGE_BACKEND=s3 docker compose --profile s3 up -d --wait
