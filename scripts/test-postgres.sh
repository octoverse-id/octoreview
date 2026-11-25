#!/usr/bin/env bash
# Run the pytest suite (or another command) against a throwaway PostgreSQL 17 container.
# Usage: scripts/test-postgres.sh <container-name> <host-port> <database-url> [command...]
set -euo pipefail
cd "$(dirname "$0")/.."
container=$1
port=$2
url=$3
shift 3
if [ "$#" -eq 0 ]; then set -- uv run pytest; fi

docker container inspect "$container" >/dev/null 2>&1 && docker container stop "$container" >/dev/null
docker run -d --rm --name "$container" -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=octoreview \
  -p "$port:5432" postgres:17-alpine >/dev/null
trap 'docker container stop "$container" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 30); do
  if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 1
done

DATABASE_URL=$url "$@"
