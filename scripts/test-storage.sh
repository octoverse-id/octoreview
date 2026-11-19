#!/usr/bin/env bash
# Run the S3 tests (`pytest -m s3`) and `check_storage` against a throwaway S3-compatible
# emulator (RustFS; docs/storage.md records why and how it was chosen).
# Usage: scripts/test-storage.sh <container-name> <host-port> <image>
set -euo pipefail
cd "$(dirname "$0")/.."
container=$1
port=$2
image=$3

access_key=octoreview-test
secret_key=$(uv run python -c 'import secrets; print(secrets.token_hex(16))')
docker container inspect "$container" >/dev/null 2>&1 && docker container stop "$container" >/dev/null
docker run -d --rm --name "$container" -e RUSTFS_ACCESS_KEY="$access_key" \
  -e RUSTFS_SECRET_KEY="$secret_key" -p "$port:9000" "$image" >/dev/null
trap 'docker container stop "$container" >/dev/null 2>&1 || true' EXIT

export AWS_ACCESS_KEY_ID=$access_key AWS_SECRET_ACCESS_KEY=$secret_key AWS_DEFAULT_REGION=us-east-1
export OCTOREVIEW_TEST_S3_ENDPOINT_URL=http://localhost:$port

for attempt in $(seq 1 60); do
  if uv run python -c "import boto3, sys; boto3.client('s3', endpoint_url=sys.argv[1]).list_buckets()" \
    "$OCTOREVIEW_TEST_S3_ENDPOINT_URL" >/dev/null 2>&1; then break; fi
  if [ "$attempt" = 60 ]; then echo "test-storage: the emulator did not start" >&2; exit 1; fi
  sleep 1
done
# Fail rather than skip when the emulator is unreachable, and include the 500 MiB completion.
export OCTOREVIEW_TEST_S3_REQUIRED=1 OCTOREVIEW_TEST_S3_LARGE=${OCTOREVIEW_TEST_S3_LARGE:-1}
uv run pytest -m s3 --no-cov

# The command as an operator runs it: settings from the environment, the default credential chain.
bucket=octoreview-check
uv run python - "$OCTOREVIEW_TEST_S3_ENDPOINT_URL" "$bucket" <<'PY'
import sys

import boto3

endpoint, bucket = sys.argv[1:3]
client = boto3.client("s3", endpoint_url=endpoint)
client.create_bucket(Bucket=bucket)
# The 1-day `_staging/` rule from docs/storage.md, so this run sees a correctly set up bucket.
rule = {"ID": "octoreview-staging", "Filter": {"Prefix": "_staging/"}, "Status": "Enabled"}
rule["Expiration"] = {"Days": 1}
client.put_bucket_lifecycle_configuration(
    Bucket=bucket, LifecycleConfiguration={"Rules": [rule]}
)
# The CORS rule from docs/storage.md for the origin config/settings_pytest.py configures.
cors = {"AllowedOrigins": ["https://shop.example"], "AllowedMethods": ["GET", "PUT"]}
cors |= {"AllowedHeaders": ["content-type", "range"], "MaxAgeSeconds": 600}
cors["ExposeHeaders"] = ["ETag", "Content-Length", "Content-Range"]
client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [cors]})
PY
DJANGO_SETTINGS_MODULE=config.settings_pytest OCTOREVIEW_STORAGE_BACKEND=s3 \
  OCTOREVIEW_STORAGE_S3_BUCKET=$bucket OCTOREVIEW_STORAGE_S3_REGION=us-east-1 \
  OCTOREVIEW_STORAGE_S3_ENDPOINT_URL=$OCTOREVIEW_TEST_S3_ENDPOINT_URL \
  OCTOREVIEW_STORAGE_S3_ADDRESSING_STYLE=path uv run python manage.py check_storage
