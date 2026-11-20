#!/usr/bin/env bash
# Smoke-test a built server image (`make docker-smoke`; the CI `docker` job runs the same).
# Usage: scripts/docker-smoke.sh <image>
# Names the failing step. Uses throwaway secrets and a SQLite database inside the container.
set -euo pipefail
image=$1
port=${DOCKER_SMOKE_PORT:-58000}
name=octoreview-docker-smoke-$$

env_args=(
  -e DJANGO_SECRET_KEY=docker-smoke-not-a-real-secret
  -e OCTOREVIEW_SERVICE_TOKEN_PEPPER=docker-smoke-pepper
  -e OCTOREVIEW_ERASURE_HMAC_KEY=docker-smoke-erasure-key
  -e OCTOREVIEW_STORAGE_LOCATION_ID=docker-smoke
  -e OCTOREVIEW_PUBLIC_BASE_URL="http://localhost:$port"
  -e OCTOREVIEW_MEDIA_ROOT=/var/lib/octoreview/media
  -e OCTOREVIEW_MEDIA_TOKEN_SECRET=docker-smoke-media-secret
  -e DATABASE_URL=sqlite:////tmp/octoreview-smoke.sqlite3
  -e DJANGO_ALLOWED_HOSTS=localhost
)

fail() {
  echo "docker-smoke: FAILED at $1" >&2
  if docker container inspect "$name" >/dev/null 2>&1; then docker logs "$name" >&2 || true; fi
  exit 1
}
ok() { echo "  $1: ok"; }
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

uid=$(docker run --rm --entrypoint id "$image" -u) || fail "non_root"
[ "$uid" = 1001 ] || fail "non_root (uid $uid)"
ok non_root

docker run --rm --entrypoint python "$image" -c "import boto3" || fail "s3_extra"
ok s3_extra

if docker run --rm --entrypoint python "$image" -c "import pytest" 2>/dev/null; then
  fail "no_dev_tools (pytest is installed)"
fi
ok no_dev_tools

docker run --rm "${env_args[@]}" "$image" python manage.py check >/dev/null || fail "manage_check"
ok manage_check

# The entrypoint refuses to start a misconfigured container (here: no secrets, DEBUG off).
if docker run --rm -e DATABASE_URL=sqlite:////tmp/x.sqlite3 "$image" true >/dev/null 2>&1; then
  fail "fail_closed (a container without secrets started)"
fi
ok fail_closed

docker run -d --name "$name" "${env_args[@]}" -p "$port:8000" "$image" \
  sh -c 'python manage.py migrate --noinput >/dev/null && exec gunicorn config.wsgi:application' \
  >/dev/null || fail "start"
for path in live ready; do
  served=0
  for _ in $(seq 1 30); do
    if [ -z "$(docker ps -q --filter "name=^${name}$")" ]; then fail "start (the container exited)"; fi
    if curl -fsS "http://localhost:$port/api/v1/health/$path" >/dev/null 2>&1; then
      served=1
      break
    fi
    sleep 1
  done
  [ "$served" = 1 ] || fail "health_$path"
  ok "health_$path"
done

status=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$port/admin/")
[ "$status" = 404 ] || fail "admin_off (HTTP $status; the console must be opt-in)"
ok admin_off

docker logs "$name" 2>&1 | grep -q '"Using worker: gthread"' || fail "gthread"
ok gthread
# Every log line is JSON (docs/spec.md section 16): no plain-text banner leaks into the stream.
docker logs "$name" 2>&1 | python3 -c '
import json, sys
for line in sys.stdin:
    if line.strip():
        json.loads(line)
' || fail "json_logs"
ok json_logs
# The console, when enabled, renders and its collected static files serve through WhiteNoise.
admin=$name-admin
admin_port=$((port + 1))
trap 'docker rm -f "$name" "$admin" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$admin" "${env_args[@]}" -e OCTOREVIEW_ADMIN_ENABLED=true \
  -e OCTOREVIEW_PUBLIC_BASE_URL="http://localhost:$admin_port" -p "$admin_port:8000" "$image" \
  >/dev/null || fail "admin_start"
login=""
for _ in $(seq 1 30); do
  if login=$(curl -fsS "http://localhost:$admin_port/admin/login/" 2>/dev/null); then break; fi
  sleep 1
done
[ -n "$login" ] || fail "admin_login"
ok admin_login
stylesheet=$(printf '%s' "$login" | grep -o '/static/[^"]*\.css' | head -n1)
[ -n "$stylesheet" ] || fail "admin_static (the login page links no stylesheet)"
status=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$admin_port$stylesheet")
[ "$status" = 200 ] || fail "admin_static ($stylesheet answered $status)"
ok admin_static
echo "docker-smoke: ok ($image)"
