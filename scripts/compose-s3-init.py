"""The compose `s3` profile's init step: set up the emulator bucket as docs/storage.md describes.

Creates the bucket, the 1-day `_staging/` lifecycle rule and a CORS rule for the browser origins
in OCTOREVIEW_MEDIA_CORS_ORIGINS. Idempotent; waits up to a minute for the emulator to answer.
"""

from __future__ import annotations

import os
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

endpoint = os.environ["OCTOREVIEW_STORAGE_S3_ENDPOINT_URL"]
bucket = os.environ["OCTOREVIEW_STORAGE_S3_BUCKET"]
origins = [
    o.strip() for o in os.getenv("OCTOREVIEW_MEDIA_CORS_ORIGINS", "").split(",") if o.strip()
]
client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    region_name=os.getenv("OCTOREVIEW_STORAGE_S3_REGION", "us-east-1"),
    config=Config(s3={"addressing_style": "path"}),
)

deadline = time.monotonic() + 60
while True:
    try:
        existing = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
        break
    except (BotoCoreError, ClientError) as exc:
        if time.monotonic() > deadline:
            sys.exit(f"compose-s3-init: the emulator at {endpoint} did not answer: {exc}")
        time.sleep(1)

if bucket not in existing:
    client.create_bucket(Bucket=bucket)
client.put_bucket_lifecycle_configuration(
    Bucket=bucket,
    LifecycleConfiguration={
        "Rules": [
            {
                "ID": "octoreview-staging",
                "Filter": {"Prefix": "_staging/"},
                "Status": "Enabled",
                "Expiration": {"Days": 1},
            }
        ]
    },
)
if origins:
    client.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": origins,
                    "AllowedMethods": ["GET", "PUT"],
                    "AllowedHeaders": ["content-type", "range"],
                    "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
                    "MaxAgeSeconds": 600,
                }
            ]
        },
    )
print(f"compose-s3-init: bucket {bucket} ready (staging lifecycle rule, CORS for {origins})")
