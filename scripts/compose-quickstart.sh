#!/usr/bin/env bash
# The `compose-quickstart` check (CI; `make compose-quickstart`; docs/spec.md section 19).
# Three runs, each from start through the curl quickstart and the smoke test within the budget:
#   1. a clean stack on the local backend through `make up`, the README's entry point;
#   2. a second run against the same volumes with `docker compose up` (the seed is idempotent,
#      the data persists);
#   3. a clean stack on the `s3` profile (RustFS) through `make up-s3`.
# Runs 1 and 3 fail when `make up` did not copy .octoreview-dev.env out. The image build is timed
# separately. Results go to $GITHUB_STEP_SUMMARY when set. It overwrites .octoreview-dev.env and
# tears the stack down with its volumes, so do not point it at a stack you want to keep.
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

# run NAME ENV_FILE COMMAND...: start the stack with COMMAND, which must leave the demo key in
# ENV_FILE, then run the quickstart and the smoke test against it.
run() {
  local name=$1 file=$2
  shift 2
  local started
  started=$(date +%s)
  rm -f "$file"
  "$@"
  [ -s "$file" ] || { echo "compose-quickstart: $name did not write $file" >&2; exit 1; }
  scripts/quickstart.sh "$file"
  # The deployment smoke (docs/operations.md) against the same stack, cleaning up after itself.
  OCTOREVIEW_SMOKE_APPLICATION_ID=storefront scripts/smoke-upload.sh "$file"
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

# Run 2 keeps the plain `docker compose up` and copies the file out itself.
up_and_copy() {
  docker compose up -d --wait
  docker compose cp octoreview:/var/lib/octoreview/dev/.octoreview-dev.env "$env_file" >/dev/null
}

run "local backend, clean" .octoreview-dev.env make up
down
run "local backend, second run" "$env_file" up_and_copy
down -v
run "s3 profile, clean" .octoreview-dev.env make up-s3
