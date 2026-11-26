#!/usr/bin/env bash
# The curl quickstart (README; CI `compose-quickstart`): create a review, upload a photo through
# intent, PUT and complete, then read the rating summary and fetch the photo URL.
# Usage: scripts/quickstart.sh [path/to/.octoreview-dev.env]   (make up or make run writes it)
# Names the failed step and its request id; never prints the key.
set -euo pipefail
env_file=${1:-.octoreview-dev.env}
if [ ! -f "$env_file" ]; then
  echo "quickstart: $env_file is missing; with Docker run make up, without Docker start make run (both write it)" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$env_file"
base=$OCTOREVIEW_BASE_URL
app=$OCTOREVIEW_APPLICATION_ID
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail() {
  echo "quickstart: FAILED at $1 (HTTP $2, request id $3)" >&2
  # awk, not sed: it ends the last line of a body without a newline, so the hint starts on its own.
  awk '{ print "  " $0 }' "$work/body" >&2 || true
  if [ "$2" = 401 ]; then
    # Each path has its own database, so a key seeded by one is unknown to the other.
    echo "quickstart: the key in $env_file is not live on $base; with Docker run make dev-env, without Docker restart make run" >&2
  fi
  exit 1
}

# api METHOD PATH [JSON] -> the response body in $work/body; fails the step on a non-2xx.
api() {
  local step=$1 method=$2 path=$3 data=${4:-}
  local args=(-sS -o "$work/body" -D "$work/headers" -w '%{http_code}' -X "$method"
    -H "Authorization: Bearer $OCTOREVIEW_API_KEY" -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  local status
  status=$(curl "${args[@]}" "$base/api/v1$path")
  if [ "${status:0:1}" != 2 ]; then
    fail "$step" "$status" "$(grep -i '^x-request-id:' "$work/headers" | tr -d '\r' | cut -d' ' -f2)"
  fi
}
field() { python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2], {"d": d}))' "$work/body" "$1"; }

# A real 1x1 PNG, so the photo also renders in a browser.
python3 - "$work/photo.png" <<'PY'
import struct, sys, zlib
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"\x00\xff\x99\x00")) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY
size=$(wc -c <"$work/photo.png" | tr -d ' ')
author="quickstart-$(date +%s)-$RANDOM"

api create_review POST /reviews "{\"application_id\": \"$app\", \"entity_type\": \"product\",
  \"external_id\": \"sku-quickstart\", \"author_id\": \"$author\", \"rating\": 5,
  \"title\": \"Fits perfectly\", \"body\": \"Sent with the octoreview quickstart.\"}"
review_id=$(field 'd["data"]["id"]')
entity_id=$(field 'd["data"]["entity_id"]')

api create_upload POST "/reviews/$review_id/media?application_id=$app" \
  "{\"kind\": \"image\", \"content_type\": \"image/png\", \"size_bytes\": $size}"
media_id=$(field 'd["data"]["media"]["id"]')
upload_url=$(field 'd["data"]["upload"]["url"]')
put_args=(-sS -o "$work/body" -w '%{http_code}' -X PUT --data-binary "@$work/photo.png")
# A read loop, because stock macOS ships bash 3.2. The headers go straight into put_args, which
# is never empty, so no empty array is expanded under set -u (bash before 4.4 refuses that).
while IFS= read -r header; do
  if [ -n "$header" ]; then put_args+=(-H "$header"); fi
done < <(field '"\n".join(f"{k}: {v}" for k, v in d["data"]["upload"]["headers"].items())')
status=$(curl "${put_args[@]}" "$upload_url")
[ "${status:0:1}" = 2 ] || fail upload_put "$status" "-"

api complete POST "/reviews/$review_id/media/$media_id/complete?application_id=$app" '{}'
api rating GET "/entities/$entity_id/rating?application_id=$app"
rating=$(field '"average {} from {} published review(s)".format(d["data"]["rating_average"], d["data"]["rating_count"])')
api review GET "/reviews/$review_id?application_id=$app"
photo_url=$(field 'd["data"]["media"][0]["url"]')
status=$(curl -sS -o "$work/served.png" -w '%{http_code}' "$photo_url")
[ "$status" = 200 ] && cmp -s "$work/photo.png" "$work/served.png" || fail photo_get "$status" "-"

echo "review: $review_id"
echo "rating: $rating"
echo "photo: $photo_url"
