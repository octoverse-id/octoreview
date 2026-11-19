# Storage

How to set up the bucket or directory that holds review media. [`spec.md`](spec.md) section 7 is
the authority on behavior; this page is the operator guide. `OCTOREVIEW_STORAGE_BACKEND` selects
one backend per deployment:

| Backend | Use | Bytes flow |
| --- | --- | --- |
| `local` | development and single-node deployments | through the API process (`/api/v1/media-uploads/{token}`, `/api/v1/media-files/{token}`) |
| `s3` | AWS S3 and S3-compatible stores | straight between the client and the bucket, through presigned URLs |

Azure Blob and Google Cloud Storage arrive in release line 2.

## Local backend

- Set `OCTOREVIEW_MEDIA_ROOT` (a writable directory), `OCTOREVIEW_PUBLIC_BASE_URL` (absolute URLs
  are built from it, never from `Host`) and `OCTOREVIEW_MEDIA_TOKEN_SECRET`. Boot fails without
  them.
- Uploads stream to disk in chunks; free space is checked at intent time.
- `cleanup_media` sweeps staging files older than a day.
- `OCTOREVIEW_MEDIA_CORS_ORIGINS` lists the browser origins the two token paths answer CORS for.
- Only one node can serve the files, so use it for development or a single server.

## S3 backend

### Settings

| Variable | Notes |
| --- | --- |
| `OCTOREVIEW_STORAGE_S3_BUCKET` | required |
| `OCTOREVIEW_STORAGE_S3_REGION` | required (`us-east-1` for most emulators) |
| `OCTOREVIEW_STORAGE_S3_ENDPOINT_URL` | unset for AWS S3; the store's URL for S3-compatible stores |
| `OCTOREVIEW_STORAGE_S3_PUBLIC_ENDPOINT_URL` | the host presigned URLs are signed for, when clients reach the store by another name than the server does (a compose service); defaults to the endpoint |
| `OCTOREVIEW_STORAGE_S3_ADDRESSING_STYLE` | `auto` (default), `path` or `virtual`; use `path` for a store reached by a bare host name |
| `OCTOREVIEW_STORAGE_LOCATION_ID` | a stable id for this bucket, for example `aws-ap-southeast-3-reviews`; every media row records it |

Install the extra with `uv sync --extra s3`; the published image already has it.

### Credentials

The backend uses the boto3 default credential chain: environment keys, a shared profile, IRSA on
EKS, or an instance or task role. Prefer a role over long-lived keys.

Temporary credentials (IRSA, roles) expire. No URL outlives the credential that signed it: URLs
end 60 seconds before the credential does. When the credential cannot sign a URL that lives at
least 60 seconds, the API answers 503 `storage_credentials_expiring` with `Retry-After`. botocore
refreshes role credentials well before that point, so this is rare and the retry succeeds.

### IAM policy

The role that runs octoreview signs every URL, so it needs the permissions the URLs use:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MediaObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::REVIEWS-BUCKET/*"
    },
    {
      "Sid": "MissingKeysAnswer404",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::REVIEWS-BUCKET"
    },
    {
      "Sid": "CheckStorageReadsLifecycleRules",
      "Effect": "Allow",
      "Action": "s3:GetLifecycleConfiguration",
      "Resource": "arn:aws:s3:::REVIEWS-BUCKET"
    }
  ]
}
```

- `s3:ListBucket` matters: without it S3 answers 403 instead of 404 for a missing key, and
  `complete` reports `storage_unavailable` where it means `upload_not_found`. `check_storage`
  fails at `stat_missing` when it is missing.
- `s3:GetLifecycleConfiguration` is optional; without it `check_storage` warns that it cannot
  verify the lifecycle rules.
- With SSE-KMS default encryption, also grant `kms:GenerateDataKey` and `kms:Decrypt` on the key.

### Bucket setup

1. **Keep the bucket private.** Leave Block Public Access on. Readers get presigned URLs. For
   public-CDN mode, see [Read URLs](#read-urls).
2. **Expire staging after one day.** Uploads land under `_staging/`; `complete` copies them to
   `media/` and deletes the staging object, but abandoned uploads and re-PUTs stay. A lifecycle
   rule removes them:

   ```json
   {
     "Rules": [
       {
         "ID": "octoreview-staging",
         "Filter": { "Prefix": "_staging/" },
         "Status": "Enabled",
         "Expiration": { "Days": 1 }
       }
     ]
   }
   ```

   `aws s3api put-bucket-lifecycle-configuration --bucket REVIEWS-BUCKET --lifecycle-configuration file://lifecycle.json`

   Never add an expiration rule that covers `media/` (including a whole-bucket rule): it would
   delete published media. `check_storage` fails on one.
3. **Allow browser origins (CORS).** Browsers upload and play media straight from the bucket, so
   the bucket must allow your storefront origins. List the same origins in
   `OCTOREVIEW_MEDIA_CORS_ORIGINS`; `check_storage` sends a preflight for each and warns about any
   the bucket refuses.

   ```json
   {
     "CORSRules": [
       {
         "AllowedOrigins": ["https://shop.example"],
         "AllowedMethods": ["GET", "PUT"],
         "AllowedHeaders": ["content-type", "range"],
         "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
         "MaxAgeSeconds": 600
       }
     ]
   }
   ```

   `aws s3api put-bucket-cors --bucket REVIEWS-BUCKET --cors-configuration file://cors.json`
4. **Versioning.** Leave it off. With versioning on, a delete only adds a delete marker and the
   bytes stay, which breaks author erasure (`spec.md` section 9). If your organization requires
   versioning, add a `NoncurrentVersionExpiration` rule (1 day) for the whole bucket so deleted
   bytes go. S3 Object Lock is not supported: media deletes must succeed.
5. **Run `check_storage`.** See [below](#check_storage).

### Uploads

- `POST /reviews/{id}/media` returns `upload: {method: "PUT", url, headers, expires_at}`. The URL
  signs `Content-Type` and `Content-Length`: the client sends exactly `upload.headers` plus an
  explicit `Content-Length` (never chunked), and the store refuses any other size or type.
- `complete` validates the staged object (size, then magic bytes) and copies exactly that version
  to `media/...` with `CopySourceIfMatch`, setting the content type itself. A re-PUT during
  completion fails the copy with 409 `upload_changed`; the client calls `complete` again.
- Upload URLs stay valid until they expire, even after `complete`. A late PUT only rewrites the
  staging object, which the lifecycle rule removes; served bytes never change.
- **The size guarantee is `complete`.** AWS S3 and the CI emulator enforce the signed
  `Content-Length`, but some S3-compatible stores do not check signed headers. On every store,
  `complete` rejects a wrong size (422 `media_rejected`, `size_mismatch`) and deletes the object.
  `check_storage` warns at `size_cap` when the store accepted an oversize body.

### Read URLs

- **Private bucket (default).** Every media `url` is a presigned GET, valid for
  `OCTOREVIEW_MEDIA_URL_TTL_SECONDS` (default 900), capped at 7 days and by the credential
  lifetime. Range requests work, so videos seek.
- **Public-CDN mode.** Set `OCTOREVIEW_MEDIA_PUBLIC_BASE_URL` to a CDN whose origin is the bucket
  (for CloudFront, origin access control limited to `media/*`), and publicly visible media get
  `<base>/<key>` URLs: stable, cacheable and unsigned. Moderators and authors still get presigned
  URLs. Two consequences:
  - hidden or rejected media stay reachable by URL until they are deleted, and a CDN keeps them
    until purge or TTL; purge after an erasure, or keep the CDN TTL short;
  - keys contain the tenant and application ids.
- Recommend MP4 with H.264 video and AAC audio to uploaders. HEVC, the iPhone default (often
  `video/quicktime`), may not play in Chrome or Firefox; octoreview does not sniff codecs (TODOS
  PRD-8).

### Compatible stores

Octoreview needs these behaviors from the store; `check_storage` checks each on your bucket:
presigned PUT and GET (SigV4), `If-Match` on GET, `CopyObject` with `CopySourceIfMatch` and
`MetadataDirective=REPLACE`, and missing keys answering 404. The size cap, lifecycle rules and
bucket CORS are strongly recommended.

A store that ignores `CopySourceIfMatch` is not supported: it would publish whatever the staging
object holds when the copy runs, not the validated version. `check_storage` fails at
`conditional_copy` on such a store. The attempt-key fallback for those stores
(`spec.md` section 7.4) is deferred (TODOS PLT-14).

| Store | Status | Evidence |
| --- | --- | --- |
| AWS S3 | supported | documented behavior; the release smoke run (`spec.md` section 19) uses a real bucket |
| RustFS 1.0.6 | CI emulator | every check below, on every pull request |
| MinIO (Chainguard build, 2026-11-19) | protocol checks pass | spike below; no per-bucket CORS API (MinIO configures CORS server-wide) |
| versitygw | protocol checks pass | spike below; no lifecycle API, so staging needs another cleanup |
| Cloudflare R2, Wasabi, DigitalOcean Spaces, others | not tested here | run `check_storage`; it names the first missing behavior |

### Emulator

The plan named MinIO as the CI emulator. MinIO's upstream repository was archived in April 2026,
and its images left Docker Hub and Quay in September 2026, so the week-1 spike ran
`octoreview`'s storage checks against the maintained candidates on 2026-11-19:

| Check | RustFS 1.0.6 | MinIO (Chainguard) | versitygw |
| --- | --- | --- | --- |
| presigned PUT signs `content-length;content-type;host` | pass | pass | pass |
| oversize PUT refused (403) | pass | pass | pass |
| wrong `Content-Type` PUT refused (403) | pass | pass | pass |
| ranged GET with `If-Match`; stale `If-Match` refused (412) | pass | pass | pass |
| `CopySourceIfMatch` with a stale version refused (412), no object written | pass | pass | pass |
| `MetadataDirective=REPLACE` sets the content type | pass | pass | pass |
| presigned GET, with and without `Range` | pass | pass | pass |
| lifecycle rule API | pass | pass | 501 |
| bucket CORS API | pass | 501 | pass |
| CORS preflight: listed origin allowed, other origin refused | pass | — | — |
| delete of a missing key succeeds | pass | pass | pass |

RustFS (Apache-2.0) is the only candidate that passed every check, so CI runs it:
`S3_IMAGE` in the [Makefile](../Makefile) pins `rustfs/rustfs:1.0.6` by digest. `make test-storage`
starts it in Docker and runs the S3 tests (`pytest -m s3`: the shared storage contract suite, the
media API on s3 including a 500 MiB completion, and `check_storage` cases), then runs
`manage.py check_storage` against it the way an operator would. The `storage-emulators` CI job
runs the same target.

### `check_storage`

`manage.py check_storage` (`make storage-check`) round-trips a probe object and names the first
failing step. On `s3`:

| Step | Proves | Fails when |
| --- | --- | --- |
| `sign_upload` | credentials resolve and sign | no credentials, or they are nearly spent |
| `stat_missing` | a missing key answers 404 | `s3:ListBucket` is missing |
| `cors` | each `OCTOREVIEW_MEDIA_CORS_ORIGINS` origin passes the bucket's preflight | (warns) |
| `size_cap` | the store refuses a body larger than the signed size | (warns) |
| `put` | a real PUT through the presigned URL | the endpoint is unreachable from here, or `s3:PutObject` |
| `stat`, `read_head` | HEAD and ranged GET with `If-Match` | `s3:GetObject` |
| `conditional_copy` | a copy of a stale version is refused | the store ignores `CopySourceIfMatch` |
| `finalize`, `final_metadata` | the conditional copy runs and sets the server-chosen type | the store keeps the upload's metadata |
| `sign_download`, `get` | a real GET through a presigned URL | |
| `delete` | the probe is removed | `s3:DeleteObject` |
| `lifecycle` | a rule expires `_staging/` and none expires `media/` | (warns without the staging rule; fails on a media rule) |

Run it from a host that reaches the public endpoint: the PUT and GET use the presigned URLs
exactly as clients do.
