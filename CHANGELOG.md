# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The `s3` storage backend for AWS S3 and S3-compatible stores (`OCTOREVIEW_STORAGE_BACKEND=s3`,
  extra `octoreview[s3]`). Presigned PUTs sign `Content-Type` and `Content-Length`; `complete`
  copies the validated version with `CopySourceIfMatch` and server-chosen metadata; reads are
  presigned GETs. URLs never outlive the signing credential (503
  `storage_credentials_expiring`). `check_storage` gains real HTTP through the signed URLs and the
  `stat_missing`, `cors`, `size_cap`, `conditional_copy`, `final_metadata` and `lifecycle` steps.
  `docs/storage.md` is the operator guide (IAM policy, bucket CORS, lifecycle rule, compatible
  stores and the emulator spike). `make test-storage` and the `storage-emulators` CI job run the
  S3 tests against a digest-pinned RustFS emulator.

### Changed

- The local token paths (`/media-uploads/{token}`, `/media-files/{token}`) answer 404 on a cloud
  backend, and media tokens refuse an empty signing secret.
- A replayed upload intent's URL is capped by the signing credential's lifetime, like a new one.

- The API contract, frozen for the SDKs: `docs/openapi.yaml` (OpenAPI 3.1, every `/api/v1`
  operation with request and response schemas and the full error-code enum), `docs/errors.md`
  (one section per error code with status, details, fix and recovery), `docs/events.md` (envelope,
  webhook signature, one payload per event type, the erasure purge rule) and
  `docs/sdk-surface.yaml` with the generated `docs/sdk-surface.md` (Go, PHP and JS method names,
  retry classes, parameters). `make openapi-check` (part of `make checks`) compares them with the
  URLconf and the error and event registries; a conformance test validates a real response of
  every operation against its schema. `make sdk-surface` regenerates the table.
- `not_found` errors name the scope the lookup searched (`details.scope`).
- `review.published` always carries `previous_status` (null for a review published at creation).

- Review media on the local backend: upload intents (`POST /api/v1/reviews/{id}/media`) with
  per-entity image and video limits, the allowed-type list, size caps, `client_upload_id` replay
  with a fresh upload URL, and the optional per-application `max_storage_bytes` quota (computed
  `stored_bytes`/`reserved_bytes` on the settings response). Signed local `PUT
  /api/v1/media-uploads/{token}`; idempotent three-phase `complete` with a lease, magic-byte
  checks and a grace window; `GET` and `DELETE` media; media `approve`/`reject` with the version
  guard; media embedded in review responses with per-audience URLs (public, author, moderator)
  and `media_truncated`; the `media_moderation_status` moderator filter; `GET
  /api/v1/media-files/{token}` with Range, revocation and `nosniff`/CSP/CORP headers; CORS on the
  token paths only. `manage.py cleanup_media` (expired uploads, removed reviews, `deleting`
  retries, staging sweep), `manage.py check_storage` (`make storage-check`), erasure deleting the
  author's media, and `recompute_ratings` now also rebuilding `ready_media_count`.
- Reviews: `POST/GET /api/v1/reviews` (auto-creates the entity; `source_ref` and
  `is_verified_purchase` need `reviews:attest`), `GET/PATCH/DELETE /api/v1/reviews/{id}`, publish,
  reject and hide with the `expected_version`/`expected_media_version` guard, pending revisions
  under pre-moderation (`GET .../revision`, approve, reject), published-only lists with the author
  path and the moderator queue, thin review events and `entity.rating_changed`.
  `POST /api/v1/authors/erase` and `manage.py erase_author` (batched, pseudonymised author ids,
  audit redaction, `author.erased`). `manage.py recompute_ratings [--check]`.
- Entities: `PUT /api/v1/entities` (upsert that never reactivates), list, lookup, detail,
  `PATCH` and deactivation, bounded to the caller's applications and namespace partition, with a
  namespaced entity winning over a global one under `include_global=true`. Rating summaries
  (`GET /api/v1/entities/{id}/rating`) and batch summaries (`POST /api/v1/entities/ratings/query`,
  1-100 ids, constant query count). `GET/PUT /api/v1/applications/{id}/settings` with ceilings and
  an application-wide grant. Media policy resolution (entity → application → server default).
- Django project foundation: service keys (`ocr_` prefix, peppered HMAC) with tenant,
  application and namespace grants; the error envelope with `doc_url` and `retryable`; request
  ids validated to 100 characters; limit/offset pagination (max 100, offset cap 10000); the audit
  log and `GET /api/v1/audit-logs`; the transactional outbox with an envelope-only logging
  transport, timestamped webhook signatures, `dispatch_outbox_events --retry-failed` and
  `prune_outbox_events`; `GET /api/v1/health/live` and `/health/ready`.
- `docs/spec.md`, the spec of record for release line 1, with a contradiction table and the spec
  decisions it settles.
- Repository bootstrap: Apache-2.0 license, governance files (AGENTS.md, CONTRIBUTING,
  SECURITY, Code of Conduct), issue forms, PR template, Dependabot with auto-merge for minor and
  patch updates, `llms.txt` with a link check, rulesets mirror and export script, and a CI
  skeleton whose required job names are final (placeholder jobs are replaced as code lands).
