#!/usr/bin/env bash
# Smoke-test a deployed instance (docs/spec.md section 16; docs/operations.md "Smoke test"):
# health, create review, upload intent, PUT, complete, then fetch the media URL and compare the
# bytes. It deletes what it creates (the media item, then the review), names the failed step and
# its request id, and never prints the key.
#
# Environment:
#   OCTOREVIEW_BASE_URL         the server, e.g. https://reviews.example.com
#   OCTOREVIEW_API_KEY          a key for the smoke application with reviews:read,
#                               reviews:write and media:write
#   OCTOREVIEW_TENANT_ID        the tenant the key is granted
#   OCTOREVIEW_SMOKE_APPLICATION_ID  a dedicated application for smoke runs (default: smoke)
# Usage: scripts/smoke-upload.sh [ENV_FILE]   (an env file with those variables is sourced first)
set -euo pipefail
if [ "${1:-}" ]; then
  # shellcheck disable=SC1090
  . "$1"
fi
: "${OCTOREVIEW_BASE_URL:?set OCTOREVIEW_BASE_URL}"
: "${OCTOREVIEW_API_KEY:?set OCTOREVIEW_API_KEY}"
: "${OCTOREVIEW_TENANT_ID:?set OCTOREVIEW_TENANT_ID}"
base=${OCTOREVIEW_BASE_URL%/}
app=${OCTOREVIEW_SMOKE_APPLICATION_ID:-smoke}
work=$(mktemp -d)
review_id=""
media_id=""

request_id() { grep -i '^x-request-id:' "$work/headers" 2>/dev/null | tr -d '\r' | cut -d' ' -f2; }

# api STEP METHOD PATH [JSON]: the body lands in $work/body; a non-2xx fails STEP.
api() {
  local step=$1 method=$2 path=$3 data=${4:-} status
  local args=(-sS -o "$work/body" -D "$work/headers" -w '%{http_code}' -X "$method"
    -H "Authorization: Bearer $OCTOREVIEW_API_KEY" -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  status=$(curl "${args[@]}" "$base/api/v1$path") || status=000
  if [ "${status:0:1}" != 2 ]; then
    echo "smoke-upload: FAILED at $step (HTTP $status, request id $(request_id))" >&2
    sed -e 's/^/  /' "$work/body" >&2 2>/dev/null || true
    exit 1
  fi
}
field() {
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2], {"d": d}))' \
    "$work/body" "$1"
}

cleanup() {
  status=$?
  # Best effort, and quiet: the run's own verdict is what matters.
  if [ -n "$media_id" ]; then
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OCTOREVIEW_API_KEY" \
      -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID" \
      "$base/api/v1/reviews/$review_id/media/$media_id?application_id=$app" || true
  fi
  if [ -n "$review_id" ]; then
    curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $OCTOREVIEW_API_KEY" \
      -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID" \
      "$base/api/v1/reviews/$review_id?application_id=$app" || true
  fi
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

python3 - "$work/photo.png" <<'PY'
import struct, sys, zlib
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"\x00\x00\x99\xff")) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY
size=$(wc -c <"$work/photo.png" | tr -d ' ')
run="smoke-$(date +%s)-$RANDOM"

api health GET /health/ready
api create_review POST /reviews "{\"application_id\": \"$app\", \"entity_type\": \"smoke\",
  \"external_id\": \"$run\", \"author_id\": \"$run\", \"rating\": 5, \"title\": \"smoke\"}"
review_id=$(field 'd["data"]["id"]')
api create_upload POST "/reviews/$review_id/media?application_id=$app" \
  "{\"kind\": \"image\", \"content_type\": \"image/png\", \"size_bytes\": $size}"
media_id=$(field 'd["data"]["media"]["id"]')
upload_url=$(field 'd["data"]["upload"]["url"]')
put_args=(-sS -o "$work/body" -w '%{http_code}' -X PUT --data-binary "@$work/photo.png")
# A read loop for bash 3.2 (stock macOS); put_args is never empty, so set -u holds on bash < 4.4.
while IFS= read -r header; do
  if [ -n "$header" ]; then put_args+=(-H "$header"); fi
done < <(field '"\n".join(f"{k}: {v}" for k, v in d["data"]["upload"]["headers"].items())')
status=$(curl "${put_args[@]}" "$upload_url") || status=000
if [ "${status:0:1}" != 2 ]; then
  echo "smoke-upload: FAILED at upload_put (HTTP $status from the upload URL's host)" >&2
  exit 1
fi
api complete POST "/reviews/$review_id/media/$media_id/complete?application_id=$app" '{}'
api read_review GET "/reviews/$review_id?application_id=$app"
url=$(field 'next(m["url"] for m in d["data"]["media"] if m["id"] == "'"$media_id"'")')
status=$(curl -sS -o "$work/served.png" -w '%{http_code}' "$url") || status=000
if [ "$status" != 200 ] || ! cmp -s "$work/photo.png" "$work/served.png"; then
  echo "smoke-upload: FAILED at media_fetch (HTTP $status from the media URL's host)" >&2
  exit 1
fi
echo "smoke-upload: ok (review, upload, complete and media fetch against $base; cleaned up)"
