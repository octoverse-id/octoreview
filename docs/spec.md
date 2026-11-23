# Octoreview specification (spec of record)

This file is the single description of what Octoreview does. It consolidates the approved plan
(reviewed with /autoplan, approved at gate rev 4 on 2026-11-18) into one value per item. When this
file disagrees with any other document, including `AGENTS.md`, the README, a docstring or the
plan, this file wins; fix the other document. A change of behavior changes this file first, in the
same PR as the code.

[Section 21](#21-contradiction-table) lists every item the plan stated more than once with
different values, the single value that holds, and where it came from. [Section 22](#22-spec-decisions)
lists the values the plan left open that this file settles.

Release lines:

- **Line 1** (first consumer: Mbizmarket, a Go, PHP and JavaScript stack whose media go to AWS S3
  or an S3-compatible store): this server with the `local` and `s3` storage backends, and the Go,
  PHP and JavaScript SDKs. Server `1.0.0-rc.N` until the Mbizmarket integration passes, then
  `1.0.0`; SDKs `0.x`.
- **Line 2**: Azure Blob and native GCS backends, each "beta" until its real-cloud smoke run
  passes. The contract in this file is frozen in line 1 for every backend, so line 2 adds no
  breaking change.

---

## 1. Scoping and identity

| Concept | Rule |
| --- | --- |
| Tenant | `X-Tenant-ID` header, required on every authenticated request. Matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`. Every tenant-owned row carries `tenant_id`, and every query filters on it. |
| Application | `application_id`, same pattern as the tenant. Required on entities, reviews and media. Travels in the body for writes, in the query for reads, and in the path for settings. If the path, body and query disagree, the request gets 400 `application_mismatch`. |
| Namespace | Optional `X-Namespace-Type` + `X-Namespace-ID` headers: both or neither, else 400 `invalid_namespace`. Each is at most 100 characters, case-sensitive and never trimmed. `global` is a reserved `namespace_type`. Both null on a row means global. A namespaced entity requires `application_id`. |
| Namespace reads | A namespaced read excludes global rows unless `include_global=true`. `include_global` on a write is a 400 `validation_error`. When the grant does not cover the global scope, `include_global=true` silently excludes global rows. |
| Namespace writes | A namespaced write that references a global entity (for example `POST /reviews` with a global `entity_id`) fails the reference lookup with 404 `not_found`. "Write" means a mutating operation, not an HTTP method: `POST /entities/ratings/query` is a read. |
| Entity | An external resource identified by `(entity_type, external_id)` inside a tenant, application and namespace. `entity_type` matches `^[a-z0-9][a-z0-9_.-]{0,99}$` at creation; `external_id` is 1-255 characters with no control characters. |
| Author | `author_id` (at most 255 characters) is owned by the caller. A grant holder is trusted: an `author_id` filter selects data, it does not prove ownership. Integrators derive `author_id` from their own authenticated user. |
| Actor | Optional `X-Actor-ID` (at most 255 characters) is recorded in the audit log. |
| Request id | Every response, errors included, carries `X-Request-ID`. An inbound value is kept only when it matches `^[A-Za-z0-9._:-]{1,100}$`; otherwise the server generates `req_<uuid4 hex>`. |

Octoreview stores reviews, ratings, review media and per-entity review configuration. It never
stores external entity data (names, prices, descriptions stay with the caller).

## 2. Authentication and authorization

- **Service keys.** Format `ocr_<prefix>_<secret>`: `prefix` is 8 hex characters (unique), and
  `secret` is a 32-byte URL-safe token. Keys longer than 200 characters are rejected before
  hashing. Only a peppered HMAC-SHA256 of the full key is stored (`OCTOREVIEW_SERVICE_TOKEN_PEPPER`).
  Keys are sent as `Authorization: Bearer <key>` and are never echoed. Ported from octonomy
  `service_auth`.
- **401 codes.** `authentication_required` (no or malformed header) and `invalid_api_key`
  (unknown, revoked or expired key).
- **Grants** (`ServiceClientGrant`): `tenant_id`, `application_id` (null = every application in
  the tenant), a namespace scope (global-only, exact `(namespace_type, namespace_id)`, or
  `namespace_wildcard`), and `scopes`. A global-only grant cannot reach a namespaced request (403).
  An exact-namespace grant cannot see global rows.
- **Scopes:** `entities:read`, `entities:write`, `reviews:read`, `reviews:write`,
  `reviews:moderate`, `reviews:attest`, `media:write`, `settings:read`, `settings:write`,
  `audit:read`, `privacy:erase`.
- **One predicate.** Secondary permissions (`reviews:attest`, `reviews:moderate`,
  `privacy:erase`, visibility of non-published content) go through the same grant predicate as
  primary scopes: tenant, application, namespace and capability together. A key that can write in
  application A and attest only in B is refused attestation in A.
- **Application-wide resources.** `GET/PUT /applications/{id}/settings` and `POST /authors/erase`
  require a global-only or `namespace_wildcard` grant; an exact-namespace grant gets 403.
  `GET /audit-logs` with an exact-namespace grant returns only that namespace's rows.
- **403 details:** `reason`, `required_scope`, `grant_scope`.
- **Least-privilege keys for Mbizmarket** (documented in `docs/api.md`):
  - The backend that submits reviews: `reviews:write`, `media:write`, `entities:read`, plus
    `reviews:attest` when it submits post-purchase reviews.
  - The moderation console: `reviews:read`, `reviews:moderate`.
  - Privacy tooling: `privacy:erase`.

## 3. Domain model

PostgreSQL is the production database. CI runs the suite on SQLite and on PostgreSQL; row-lock
concurrency tests and covering/partial-index checks are PostgreSQL-only. All primary keys are
UUIDs. JSON columns (`metadata`, `author_metadata`) are at most 16 KiB and at most 5 levels deep. In
PATCH, `metadata` replaces the whole object.

### 3.1 ApplicationSettings

One row per `(tenant_id, application_id)`, created lazily with `get_or_create` in a savepoint. A
missing row and a row with all-null fields behave the same.

| Column | Type | Rule |
| --- | --- | --- |
| `max_images`, `max_videos` | int, nullable | null = the env default; bounded by the ceilings (section 6.1) |
| `max_image_bytes`, `max_video_bytes` | bigint, nullable | null = the env default; bounded by the ceilings |
| `allowed_content_types` | JSON list, nullable | a subset of the six allowed types; null = all six |
| `moderation_mode` | `auto_publish` \| `pre_moderation`, nullable | null = `auto_publish` |
| `max_storage_bytes` | bigint, nullable | null = unlimited |

`PUT` requires every field; an omitted field is a 400 that lists the missing ones, and explicit
`null` means "inherit". `GET` also returns `stored_bytes` and `reserved_bytes`, computed (section 7.6).

### 3.2 Entity

| Column | Rule |
| --- | --- |
| `tenant_id`, `application_id`, `namespace_type`, `namespace_id` | scope; a CHECK constraint rejects one namespace column set without the other |
| `entity_type`, `external_id` | identity (section 1) |
| `metadata` | JSON |
| `is_active` | bool, default true |
| `max_images`, `max_videos`, `max_image_bytes`, `max_video_bytes` | media policy overrides, nullable (null = inherit); `null` in PATCH clears an override |
| `rating_count`, `rating_sum`, `rating_1_count` .. `rating_5_count`, `verified_review_count` | aggregates over **published** reviews; five integer histogram columns, never an array |

Uniqueness uses octonomy's split partial unique constraints: one on
`(tenant_id, application_id, entity_type, external_id)` for global rows, and one with the namespace
columns for namespaced rows. SQLite and PostgreSQL therefore enforce the same rule, without
`NULLS NOT DISTINCT`.

### 3.3 Review

| Column | Rule |
| --- | --- |
| scope columns | copied from the entity |
| `entity` | FK |
| `author_id` | at most 255 |
| `author_metadata` | JSON (`display_name`, `avatar_url`, which must be https), caller-supplied, optional |
| `rating` | smallint, CHECK 1..5 |
| `title` | at most 200, optional |
| `body` | at most 10000, optional |
| `status` | `pending` \| `published` \| `rejected` \| `hidden` \| `removed` |
| `status_reason` | optional reason from `reject`/`hide` |
| `metadata` | JSON |
| `source_ref` | at most 255, nullable (an order or booking id) |
| `is_verified_purchase` | bool, default false |
| `version` | int, starts at 1 (section 4.4) |
| `media_version` | int, starts at 1 (section 4.4) |
| `ready_media_count` | int (section 3.5) |
| `published_at`, `created_at`, `updated_at` | timestamps |

`title` and `body` are plain text. The server rejects NUL and C0 control characters other than
newline and tab (400), and stores everything else verbatim. Consumers escape on render.

There are two partial unique constraints on non-removed rows:
- `(entity_id, author_id)` where `source_ref IS NULL`;
- `(entity_id, author_id, source_ref)` where `source_ref IS NOT NULL`.

Both partial constraints are named, and a vendor-aware `IntegrityError` classifier maps them to
409 `review_exists` with `details.review_id`. It reads PostgreSQL `diag.constraint_name` and the
SQLite column-list message. Other integrity errors (for example an FK violation) are not reported
as `review_exists`.

One review per order on the same entity is intended: each repeat purchase counts once in the
aggregates. An application that wants one review per buyer never sets `source_ref`.

### 3.4 ReviewRevision

At most one per review (unique `review_id`). Columns: `rating`, `title`, `body`,
`base_version`, `created_at`. A newer edit replaces the row and gets a new `id`.
`GET /reviews/{id}/revision` (`reviews:moderate`) returns it.

### 3.5 ReviewMedia

| Column | Rule |
| --- | --- |
| scope columns, `review` FK | copied from the review |
| `kind` | `image` \| `video` |
| `status` | `pending` \| `ready` \| `invalid` \| `deleting` \| `deleted` (section 4.3) |
| `moderation_status` | `none` \| `pending` \| `approved` \| `rejected` |
| `moderation_reason` | optional |
| `storage_location` | immutable id of the configured location (section 7.1) |
| `staging_key` | set at intent |
| `storage_key` | the final key, set when `ready` |
| `content_type` | one of the six allowed types |
| `size_bytes` | bigint, at least 1 |
| `width`, `height`, `duration_seconds` | caller-declared, untrusted hints; the server never parses them |
| `position` | int |
| `client_upload_id` | optional; unique per review among non-deleted rows |
| `upload_expires_at` | timestamp |
| `completion_attempt_id`, `completion_lease_until` | the completion lease (section 7.4) |
| `cleanup_claimed_until` | the cleanup claim |
| `created_at`, `updated_at` | timestamps |

A media item is **publicly visible** when its review is `published`, its status is `ready`, and its
`moderation_status` is `none` or `approved`. `Review.ready_media_count` counts publicly visible
media. Only the lifecycle service writes it, and `recompute_ratings` rebuilds it.

### 3.6 StorageLocation

`id` (from `OCTOREVIEW_STORAGE_LOCATION_ID`), `backend`, `created_at`. Rows record every location
the deployment has used. `manage.py check --deploy` and readiness refuse to run when a media row
references an unconfigured location. Storage migration is deferred (TODOS PLT-8).

### 3.7 ServiceClient and ServiceClientGrant

These are ported from octonomy `service_auth` (section 2).

### 3.8 AuditLog

| Column | Rule |
| --- | --- |
| `tenant_id`, `application_id`, namespace columns | scope |
| `aggregate_type`, `aggregate_id` | renamed from octonomy's `entity_type`/`entity_id`, to avoid the collision with the domain Entity |
| `action` | what happened |
| `actor_id` | from `X-Actor-ID` |
| `request_id` | at most 100 |
| `operation_id` | UUID |
| `changes` | `{field: [before, after]}` |
| `metadata` | JSON |
| `created_at` | timestamp |

The log is append-only except for erasure redaction (section 9). Attestations (`source_ref`,
`is_verified_purchase`), moderation reasons and erasures each write a row. Retention is deferred
(TODOS PLT-12).

### 3.9 OutboxEvent

Ported from octonomy `events`, with these fields: scope columns, `event_type`, `resource_type`,
`resource_id`, `payload`, `metadata`, `request_id` (at most 100), `status` (`pending` \|
`processing` \| `published` \| `failed` \| `dead_letter`), attempts and retry timing, `claim_id`,
`published_at`, `created_at`. Rows are written in the same transaction as the change they describe
(transactional outbox). Payloads are thin (section 10).

### 3.10 Indexes

Every index below ships with the first migration that creates its table.

| Table | Index |
| --- | --- |
| reviews | `(tenant_id, application_id, entity_id, status, created_at DESC, id)` |
| reviews | `(tenant_id, application_id, entity_id, status, rating DESC, created_at DESC, id)` for `sort=rating` |
| reviews | `(tenant_id, application_id, author_id)` |
| reviews | moderation queue `(tenant_id, application_id, status, created_at DESC, id)` |
| reviews | moderation queue, namespaced twin `(tenant_id, application_id, namespace_type, namespace_id, status, created_at DESC, id)` |
| review_media | `(review_id, kind, status)` |
| review_media | `(status, upload_expires_at)` |
| review_media | reservations `(tenant_id, application_id, status, upload_expires_at)` |
| review_media | stored bytes `(tenant_id, application_id, status) INCLUDE (size_bytes)` (PostgreSQL only) |
| audit_logs | `(tenant_id, actor_id)` |
| audit_logs | `(tenant_id, application_id, created_at)` |
| outbox_events | `(status, published_at)` |

## 4. State machines

All transitions live in declarative tables in `octoreview.lifecycle`, keyed by `(status, action)`.
Views and tests both read those tables. Every pair the tables omit returns 409
`invalid_transition` with `from_status`, `action` and `allowed_actions`, generated from the table.

### 4.1 Review

| From | Action | To | Scope |
| --- | --- | --- | --- |
| (create) | `auto_publish` mode | `published` | `reviews:write` |
| (create) | `pre_moderation` mode | `pending` | `reviews:write` |
| `pending` | `publish` | `published` | `reviews:moderate` |
| `pending` | `reject` | `rejected` | `reviews:moderate` |
| `published` | `hide` | `hidden` | `reviews:moderate` |
| `hidden` | `publish` | `published` | `reviews:moderate` |
| `rejected` | `publish` | `published` | `reviews:moderate` |
| any but `removed` | `remove` (`DELETE`) | `removed` | `reviews:write` |

`removed` is terminal; the same author may then create a new review. `reject` and `hide` take an
optional `reason`, stored on the row and in the audit log.

**Aggregates.** Entering `published` adds the rating, and +1 `verified_review_count` when the
review is verified. Leaving `published` subtracts both. An applied rating change, or a change of
the verified flag, while `published` adjusts the columns. Every change happens in the same
transaction, under the entity row lock, through `entities.ratings.apply_delta()`. That function is
the only writer of the aggregate columns. Inactive entities keep their aggregates current, so
moderation still adjusts them.

### 4.2 Edits and revisions

- `PATCH /reviews/{id}` changes `rating`, `title`, `body`, `metadata`, `author_metadata` and (with
  `reviews:attest`) `is_verified_purchase`. It is allowed only on `pending` and `published`
  reviews whose entity is active (else 409). It never sets or changes `source_ref` (409
  `source_ref_immutable`).
- Under `auto_publish`, and on `pending` reviews, edits apply at once.
- Under `pre_moderation`, a change to `rating`, `title` or `body` on a `published` review is stored
  as the review's single pending revision; a newer edit replaces it. The approved content and rating
  stay live and counted. The `review.revision_submitted` event is emitted.
- A pending revision is dropped on remove and on erasure, and kept on hide and across a
  moderation-mode switch.
- Changing `is_verified_purchase` in either direction needs `reviews:attest` and never triggers
  moderation.

### 4.3 Media lifecycle

| From | Event | To |
| --- | --- | --- |
| (intent) | row created | `pending` |
| `pending` | `complete` validates and commits | `ready` |
| `pending` | `complete` finds a size, type, limit or quota mismatch | `invalid` (objects deleted) |
| `pending` | `DELETE`, or cleanup after expiry | `deleting` |
| `ready` | `DELETE`, cleanup of a removed review, or erasure | `deleting` |
| `deleting` | object delete succeeds | `deleted` |

`invalid` and `deleted` are terminal and keep their rows for audit. A row in `deleting` has
`url: null` at once; `cleanup_media` retries it until the delete succeeds.

Media moderation (`reviews:moderate`):

| Action | Effect |
| --- | --- |
| `approve` | sets `moderation_status=approved` on `ready` media (from `pending` or `rejected`) |
| `reject` | sets `moderation_status=rejected` and `url: null` for non-moderators; the object is kept |

- On `complete`, media gets `moderation_status=pending` when its review is `published` and the
  application is in `pre_moderation`; otherwise it gets `none` and follows the review's visibility.
- Publishing a review does not change media moderation. There is no media `hide`.
- Moderation-rejected media still counts toward the limit until deleted.
- `DELETE /reviews/{id}/media/{mid}` works on any review status except `removed` and never
  triggers re-moderation.

### 4.4 Versions and the moderation guard

- `version` bumps only when a rating, title or body change is applied (directly, or by approving a
  revision).
- `media_version` bumps on every media state change (`ready`, `invalid`, `deleting`, `deleted`) and
  every media moderation change.
- Attestation flips and review status changes bump neither.
- `publish`, `revision/approve` and media `approve` require `expected_version` and
  `expected_media_version` (missing → 400). They return 409 `review_changed` when either differs
  from the current value, so nothing a moderator has not seen goes public.
- Conflict precedence on `revision/approve`:
  1. `revision_superseded`: the `revision_id` is not the current revision.
  2. `review_changed`: an expected version differs.
  3. `revision_stale`: the revision's `base_version` is no longer the review's `version`.

## 5. REST API (`/api/v1`)

The OpenAPI document is `docs/openapi.yaml`.

- **Single-resource envelope:** `{"data": ...}`.
- **Lists:** `{"data": [...], "pagination": {"limit", "offset", "count"}}`, with `limit` default
  20 and max 100, `offset` max 10000, ties sorted by `id`. Cursor pagination is deferred (TODOS PLT-9).
- **Routes:** `/entities/{id}` and `/reviews/{id}` use Django's `<uuid:...>` converter, so
  `/entities/lookup` and `/entities/ratings/query` never collide with them.

| Method | Path | Scope |
| --- | --- | --- |
| GET | `/health/live`, `/health/ready` (readiness checks the database, never storage) | none |
| PUT | `/entities` (upsert by `application_id, entity_type, external_id`; never reactivates) | `entities:write` |
| GET | `/entities`, `/entities/{id}`, `/entities/lookup?entity_type=&external_id=` | `entities:read` |
| PATCH | `/entities/{id}` (`metadata`, media policy overrides, `is_active`) | `entities:write` |
| DELETE | `/entities/{id}` (sets `is_active=false`) | `entities:write` |
| GET | `/entities/{id}/rating` | `entities:read` |
| POST | `/entities/ratings/query` (batch; a read) | `entities:read` |
| GET / PUT | `/applications/{application_id}/settings` | `settings:read` / `settings:write` |
| POST | `/reviews` | `reviews:write` (+ `reviews:attest` for `source_ref` or `is_verified_purchase`) |
| GET | `/reviews` | `reviews:read` (+ `reviews:moderate` for non-published statuses) |
| GET | `/reviews/{id}` | `reviews:read` |
| PATCH | `/reviews/{id}` | `reviews:write` (+ `reviews:attest` for `is_verified_purchase`) |
| DELETE | `/reviews/{id}` (status `removed`) | `reviews:write` |
| POST | `/reviews/{id}/publish`, `/reviews/{id}/reject`, `/reviews/{id}/hide` | `reviews:moderate` |
| GET | `/reviews/{id}/revision` | `reviews:moderate` |
| POST | `/reviews/{id}/revision/approve`, `/reviews/{id}/revision/reject` | `reviews:moderate` |
| POST | `/reviews/{id}/media` (upload intent) | `media:write` |
| POST | `/reviews/{id}/media/{media_id}/complete` | `media:write` |
| GET | `/reviews/{id}/media` | `reviews:read` |
| DELETE | `/reviews/{id}/media/{media_id}` | `media:write` |
| POST | `/reviews/{id}/media/{media_id}/approve`, `/reviews/{id}/media/{media_id}/reject` | `reviews:moderate` |
| POST | `/authors/erase` | `privacy:erase` (application-wide grant) |
| PUT | `/media-uploads/{token}` (local backend only) | signed token |
| GET | `/media-files/{token}` (local backend only) | signed token |
| GET | `/audit-logs` | `audit:read` |

### 5.1 Entities

- `POST /reviews` creates the entity on first use when given `entity_type` + `external_id`. It runs
  `get_or_create` in a savepoint and retries the read once on `IntegrityError`.
- On an inactive entity, new reviews (by id or by auto-create), review content edits and media
  intents return 409 `entity_inactive` with `entity_id` and the fix
  `PATCH /entities/{id} {"is_active": true}`. Reads, rating summaries and moderation keep working.
  `PATCH` with `is_active=true` reactivates; `PUT /entities` never does.
- Batch summaries:
  - **Request:** `POST /entities/ratings/query` with `{application_id, entity_type,
    external_ids: [...]}` (1 to 100 ids; 101 is a 400 `validation_error`); `include_global` goes in
    the query string.
  - **Answer:** one summary per requested id, in request order. An unknown id gets a zero-count
    summary with `entity_id: null`, not a 404. Duplicate ids are looked up once and answered at
    every position. With `include_global=true` on a namespaced request, a namespaced entity wins
    over a global one with the same key, and each summary names the matched entity's namespace.
    `GET /entities/lookup` follows the same precedence.
  - **Cost:** a constant number of SQL queries, whatever the id count.

**Rating summary** (entity rating, batch, entity detail):

| Field | Rule |
| --- | --- |
| `rating_count` | published reviews |
| `rating_average` | a JSON number with 2 decimals, `null` when the count is 0 |
| `histogram` | `{"1": n, "2": n, "3": n, "4": n, "5": n}` |
| `histogram_percent` | same keys, 1 decimal each, all zeros when the count is 0; not guaranteed to sum to 100 |
| `verified_review_count` | published verified reviews |

### 5.2 Reviews

- **Create.** `POST /reviews` takes `entity_id`, or `entity_type` + `external_id`, plus
  `author_id`, `rating`, optional `title`, `body`, `metadata` and `author_metadata`.
  - With `reviews:attest` it may also take `source_ref` and `is_verified_purchase`. Without it,
    either field gets 403. `source_ref` can be set only here.
  - A duplicate key returns 409 `review_exists` with `details.review_id`.
  - There is no `Idempotency-Key` header in v1.
- **List.** `GET /reviews` returns only `published` reviews by default.
  - **Filters:** entity (`entity_id`, or `entity_type` + `external_id`), `author_id`, `status`,
    `rating`, `has_media`, `is_verified_purchase`, `include_global`, `has_pending_revision`,
    `media_moderation_status`.
  - **Sort:** `newest` (default) or `rating`.
  - **Gated filters:** listing another status, `has_pending_revision` or `media_moderation_status`
    needs `reviews:moderate`. The author path is the exception: `author_id=<id>&include_unpublished=true`
    returns that author's non-removed reviews of any status. Anything else gets 403.
- **Detail.** `GET /reviews/{id}` returns any status with `reviews:read`, because the caller
  already holds the id.
- **Representation.** Every review includes `is_verified_purchase`, `version` and
  `media_version`, and embeds at most `OCTOREVIEW_LIST_MEDIA_LIMIT` (default 10) media. Past that
  limit it sets `media_truncated: true`, and `GET /reviews/{id}/media` returns them all.

**Media URLs in responses:**

| Caller | Gets a URL for |
| --- | --- |
| A `reviews:moderate` grant | every non-deleting, non-deleted media item, including moderation-rejected ones |
| The author path | media of the author's non-removed reviews, except moderation-rejected, deleting and deleted media |
| Everyone else | publicly visible media only |

Every other media item has `url: null`. A URL that cannot be signed is returned as `url: null` with
`url_unavailable: "signing"`; a bare `null` always means "not visible to you".

On a single-review endpoint (`GET /reviews/{id}`, `GET /reviews/{id}/media` and the media write
responses), the author path applies only when the `author_id` query parameter equals the review's
author; on a list it is already filtered to that author.

### 5.3 Caching

- **Until `v1.0.0-rc.1`:** rating summaries, batch summaries and review lists and details send
  `Cache-Control: private, max-age=60` and
  `Vary: Authorization, X-Tenant-ID, X-Namespace-Type, X-Namespace-ID`, with no `ETag`.
- **After rc.1 and before `v1.0.0`** (gate d17b), GET responses gain `ETag` and honor
  `If-None-Match` with 304:
  - Review lists and details hash the ordered `(id, version, media_version)` of the page, the total
    count and the signing epoch `floor(now / (TTL/2))`.
  - Rating summaries hash the aggregate columns.
  - The batch POST never sends validators.
- **SDKs:** they ship conditional reads (`ifNoneMatch`, a typed not-modified result) from `0.1.0`,
  so turning ETags on is additive.
- **Signed URLs vs page caching:** cached pages should use public-CDN mode (section 8).

## 6. Media policy

### 6.1 Limits

The policy for a review resolves per field: entity override, then the application settings, then
the server default.

| Variable | Default | Ceiling variable | Ceiling |
| --- | --- | --- | --- |
| `OCTOREVIEW_DEFAULT_MAX_IMAGES` | 5 | `OCTOREVIEW_MAX_IMAGES_CEILING` | 20 |
| `OCTOREVIEW_DEFAULT_MAX_VIDEOS` | 1 | `OCTOREVIEW_MAX_VIDEOS_CEILING` | 5 |
| `OCTOREVIEW_DEFAULT_MAX_IMAGE_BYTES` | 10485760 (10 MiB) | `OCTOREVIEW_MAX_IMAGE_BYTES_CEILING` | 52428800 (50 MiB) |
| `OCTOREVIEW_DEFAULT_MAX_VIDEO_BYTES` | 104857600 (100 MiB) | `OCTOREVIEW_MAX_VIDEO_BYTES_CEILING` | 524288000 (500 MiB) |

A value above its ceiling gets 400 `validation_error`. v1 has no resumable upload: one PUT per
object. Lowering a limit or narrowing `allowed_content_types` keeps existing ready media; `complete`
applies the current policy.

### 6.2 Allowed types and signatures

| Kind | Content type | Extension | Signature (checked on the first `min(size, 4096)` bytes) |
| --- | --- | --- | --- |
| image | `image/jpeg` | `.jpg` | `FF D8 FF` |
| image | `image/png` | `.png` | PNG signature |
| image | `image/webp` | `.webp` | `RIFF....WEBP` |
| video | `video/mp4` | `.mp4` | `ftyp` box whose major or compatible brand is in `isom, iso2, iso4, iso5, iso6, mp41, mp42, avc1, dash, M4V ` |
| video | `video/webm` | `.webm` | EBML header whose DocType is `webm`, parsed with a bounded EBML varint reader; `matroska` is rejected |
| video | `video/quicktime` | `.mov` | `ftyp` brand `qt  `, or a first atom `moov`, or `wide` immediately followed by `mdat`/`moov`; a bare `free`/`mdat` start is rejected |

- The detected type must equal the declared one, so MP4 and MOV never stand in for each other.
- The signature table lives in the repository; there is no libmagic dependency. No server-side
  image decoding.
- Callers convert HEIC, GIF and AVIF on the client.
- `docs/storage.md` recommends MP4 with H.264 and AAC, and states that HEVC (iPhone default) may
  not play in Chrome or Firefox. Codec sniffing is deferred (TODOS PRD-8).

### 6.3 Counting slots

The per-kind count for a review includes:
- `ready` rows;
- `pending` rows whose `effective_expiry` has not passed;
- `pending` rows whose completion lease is live.

`effective_expiry = upload_expires_at + 5 minutes`. An abandoned upload frees its slot at
`effective_expiry` without waiting for cron.

The over-limit error is 409 `media_limit_exceeded`, with details `kind`, `limit`, `ready_count`,
`pending_count`, `next_slot_frees_at` and `policy_source`.

## 7. Storage and the upload protocol

### 7.1 Backends

`OCTOREVIEW_STORAGE_BACKEND` selects one backend per deployment:

| Backend | Line | Implementation |
| --- | --- | --- |
| `local` | 1 | filesystem under `OCTOREVIEW_MEDIA_ROOT`; development and single-node only |
| `s3` | 1 | boto3; `OCTOREVIEW_STORAGE_S3_ENDPOINT_URL` makes it work with S3-compatible stores |
| `azure` | 2 | azure-storage-blob |
| `gcs` | 2 | google-cloud-storage |

- Cloud SDKs are optional extras (`octoreview[s3]`); the published image installs them all.
- Every media row records the immutable `storage_location` id (`OCTOREVIEW_STORAGE_LOCATION_ID`).
- Per-tenant backends are deferred (TODOS PLT-2).

The storage protocol lives in `octoreview.media.storage`:

```text
create_upload(key, content_type, size_bytes, expires_in) -> UploadTarget(method, url, headers, expires_at)
stat(key) -> ObjectInfo(size, content_type, version) | None     # version = ETag or generation
read_head(key, n, if_version) -> bytes
finalize(staging_key, final_key, content_type, if_version)       # conditional copy, never overwrites (7.4)
delete(key)
download_url(key, expires_in) -> str
supports_conditional_copy: bool
```

One shared contract suite runs against every backend: local, the S3-compatible CI emulator
(RustFS; [`storage.md`](storage.md) records the spike that chose it) and, in line 2, Azurite and
fake-gcs-server. [`storage.md`](storage.md) is the operator guide: IAM policy, bucket CORS,
lifecycle rules and compatible stores.

### 7.2 Keys

| Purpose | Key |
| --- | --- |
| Staging | `_staging/{tenant_id}/{application_id}/{media_id}` |
| Final | `media/{tenant_id}/{application_id}/{entity_id}/{review_id}/{media_id}.{ext}` |
| Fallback attempt (deferred with the fallback, TODOS PLT-14) | `media/{tenant_id}/{application_id}/{entity_id}/{review_id}/{media_id}-{attempt_id}.{ext}` |

- Tenant and application ids are validated at the API boundary and must start with an
  alphanumeric character, so neither can be `_staging` or `media`.
- Every other segment is a server UUID, and `ext` comes from the type map (6.2), never from a
  caller filename.
- Bucket lifecycle rules expire `_staging/` after 1 day. The compose S3 emulator's setup creates the
  rule, `check_storage` warns when it is missing, and it fails on any expiration rule that covers
  `media/`.

### 7.3 Intent

`POST /reviews/{id}/media` takes `{kind, content_type, size_bytes, client_upload_id?, position?,
width?, height?, duration_seconds?}`.

1. **Validate without locks.**
   - The review is `pending` or `published` (else 409 `review_not_accepting_media`), and its entity
     is active (else 409 `entity_inactive`).
   - `kind` matches the content type family.
   - The type is allowed (400 `content_type_not_allowed` with `allowed_content_types`).
   - `size_bytes` is between 1 and the kind's max (400 `media_too_large` with `max_bytes`).
2. **Sign first.** Generate the media id and staging key, then sign the upload target before any
   transaction. A signing failure has no side effects: 503 `storage_unavailable`, or 503
   `storage_credentials_expiring` (7.8).
3. **One short transaction, in lock order.**
   - Lock ApplicationSettings, only when `max_storage_bytes` is set.
   - Lock the review.
   - A retry with the same non-deleted `client_upload_id` replays the existing row (step 4).
   - Count slots (6.3).
   - When a quota is set, check `stored_bytes + reserved_bytes + size_bytes <= max_storage_bytes`
     (7.6), else 409 `storage_quota_exceeded`.
   - Insert the `pending` row with `upload_expires_at`.
4. **Respond** 201 with `{"data": {"media": ..., "upload": {"method": "PUT", "url", "headers",
   "expires_at"}}}`. A replay answers 200 with the same shape. The first response may have been
   lost, and it carried the only upload URL, so a replay of a `pending` row re-signs a URL for the
   same staging key, valid until the row's `upload_expires_at`. A replay of a row that is no longer
   uploadable (completed, or past `upload_expires_at`) has `upload: null`.

The upload target depends on the backend:
- **s3:** the presigned PUT signs `Content-Type` and `Content-Length`.
- **local:** `PUT /api/v1/media-uploads/{token}`.

`upload.headers` never contains `Content-Length`. Server SDKs set it explicitly (never chunked
encoding), and browsers derive it from the Blob. Cloud upload URLs stay valid until they expire,
even after `complete`; only the staging object is affected, so served bytes never change (7.4).

### 7.4 Complete

`POST /reviews/{id}/media/{media_id}/complete` is idempotent: on a `ready` row it returns 200 with
the media.

**Phase 1** (short transaction, lock order):
- The row is `pending`.
- `now <= effective_expiry`, else 409 `upload_expired` with `upload_expires_at`. The row stays for
  cleanup.
- The review still accepts media and the entity is active.
- No live lease; otherwise 409 `complete_in_progress` with `Retry-After`.
- Take the lease: `completion_attempt_id` = a new random id, and
  `completion_lease_until = now + OCTOREVIEW_COMPLETE_LEASE_SECONDS` (default 900).

**Phase 2** (no locks):
- `stat` the staging object. If it is missing: 409 `upload_not_found`, lease released, row stays
  `pending`.
- Check that the size equals `size_bytes`.
- `read_head(min(size, 4096))` with `If-Match` on the stat version, and match the signature.
- `finalize`: a conditional copy of exactly the validated version to the deterministic final key,
  with server-chosen metadata. S3 uses one `CopyObject` with `CopySourceIfMatch` and
  `MetadataDirective=REPLACE` (no multipart copy: S3 allows 5 GiB per copy and the ceiling is
  500 MiB). GCS uses `if_source_generation_match` and Azure `source_etag` (line 2). Local validates
  the staged file and then hard-links it onto the final path.
- **A published object is never overwritten.** The copy also carries `If-None-Match: *` (local:
  `link(2)`, which refuses an existing name). A copy that outlives its lease, while another attempt
  publishes, therefore cannot replace those bytes. When the final key already holds an object (an
  earlier attempt copied and then lost its lease or died), phase 2 validates that object (size,
  then magic bytes) instead and publishes it, or rejects it as below. The S3 copy client never
  retries on its own; the client repeats `complete`, which takes a fresh lease.
- A failed precondition (a re-PUT changed the staging object) returns 409 `upload_changed`. The
  lease is released, the row stays `pending`, and the client runs `complete` again.
- A storage error returns 503 `storage_unavailable` with `Retry-After`, `details.backend` and
  `details.operation`.
- The copy runs to the end inside the lease. There is no 20-second copy deadline for line-1
  backends. The SDKs' `complete` timeout defaults to the lease.

**Phase 3** (short transaction, lock order):
- The row is still `pending` and the attempt id still matches.
- The review still accepts media and the entity is active. Otherwise: 409, the row stays `pending`,
  and the final object is deleted after commit.
- Re-count slots excluding this row, and re-check the quota when one is set.
- Then mark the row `ready`, set `storage_key` and `moderation_status` (4.3), bump `media_version`,
  update `ready_media_count`, and write the audit row and the `review_media.ready` event.
- After commit, the staging object is deleted (best effort; the lifecycle rule backs it up).

**Rejection.** Only a size, type, limit or quota mismatch rejects the row. The row becomes
`invalid`, both objects are deleted after commit, `review_media.invalid` is emitted, and the
response is 422 `media_rejected` with `details.reason`, one of `size_mismatch`, `type_mismatch`,
`over_limit` or `over_quota`.

**Concurrency cap.** At most `OCTOREVIEW_MAX_CONCURRENT_COMPLETIONS` (default 4) completions run
per process; beyond that the request gets 503 `too_many_completions` with `Retry-After`. The image
runs gunicorn `gthread` with `GUNICORN_THREADS` (default 8) above that cap.

**Conditional copy is required.** The week-1 spike ([`storage.md`](storage.md)) found conditional
copy on every store it ran (RustFS, MinIO and versitygw), and AWS S3 documents it, so line 1 ships
no fallback. `check_storage` fails at its `conditional_copy` step on a store that ignores
`CopySourceIfMatch`, and such a store is unsupported. The fallback (a random attempt id and target
key persisted before the copy, and `cleanup_media` deleting the `{media_id}-*` keys no row
committed) is deferred (TODOS PLT-14).

**Proxies.** `docs/deployment.md` documents ingress and load-balancer idle timeouts (often 60 s).
A retry after a proxy 504 meets `complete_in_progress` until the lease ends.

### 7.5 Local backend

**Required settings.**
- `OCTOREVIEW_PUBLIC_BASE_URL`: absolute URLs are built from it, never from `Host`.
- `OCTOREVIEW_MEDIA_TOKEN_SECRET`: boot fails without it.
- `OCTOREVIEW_MEDIA_TOKEN_SECRET_PREVIOUS`: also accepted, for verification during rotation. A
  token's key id is a fingerprint of the secret that signed it, so tokens issued before a
  rotation keep verifying while the old value is the previous secret.

**Tokens.** HMAC-signed, bound to purpose (upload or download, with distinct salts), media id,
storage key, declared size, expiry and key id.
- Upload tokens expire at `upload_expires_at`.
- Download tokens carry an audience (`public`, `author` or `moderator`) and expire after
  `OCTOREVIEW_MEDIA_URL_TTL_SECONDS`.

**Upload: `PUT /media-uploads/{token}`.**
- A plain Django view: `@csrf_exempt`, no DRF parsers.
- It checks the token and the row state before reading the body, and requires `Content-Length`
  (411 `content_length_required`; more than `size_bytes` is 413 `upload_too_large`).
- It streams to `<staging>.part-<uuid>` in chunks, stops at `size_bytes` (413), fsyncs, and then
  runs `os.replace` onto the staging key.
- It asserts that the resolved path stays under `OCTOREVIEW_MEDIA_ROOT`.
- Free disk space is checked at intent time.
- Token errors: 403 `upload_token_invalid`, 410 `upload_token_expired`, 409
  `upload_already_completed`.

**Download: `GET /media-files/{token}`.**
- A `public` token stops working once the media is no longer publicly visible; `author` and
  `moderator` tokens stop once the media is `deleting` or `deleted`. Revoked or invisible media
  returns 404 `not_found` at once.
- Token errors: 403 `media_token_invalid`, 410 `media_token_expired`.
- Responses stream through `FileResponse`, with the allow-listed `Content-Type`,
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'`, and
  `Cross-Origin-Resource-Policy` from `OCTOREVIEW_MEDIA_CORP` (default `cross-origin`).
- Range handling:

  | Request | Response |
  | --- | --- |
  | single range (including `bytes=-N`) | 206 |
  | `bytes=-0`, a reversed range, or a start beyond EOF | 416 `range_not_satisfiable` |
  | multi-range | 200 with the full body |

**CORS.** Both paths answer CORS preflight (and add CORS headers to error responses) for the
origins in `OCTOREVIEW_MEDIA_CORS_ORIGINS` (empty = no CORS), using django-cors-headers limited to
those two paths.

`docs/deployment.md` gives proxy settings (`client_max_body_size`, `proxy_request_buffering off`)
and recommends a separate media host.

### 7.6 Quota

- `reserved_bytes` is `SUM(size_bytes)` of the application's `pending` rows that are within
  `effective_expiry` or hold a live lease.
- `stored_bytes` is `SUM(size_bytes)` of its `ready` and `deleting` rows.
- Neither is a stored counter. Both are computed only when `max_storage_bytes` is set, under the
  ApplicationSettings lock taken at intent and in phase 3. Without a quota, neither path touches
  ApplicationSettings.
- The settings response and the `storage_quota_exceeded` details report the computed values.
- The query lives in `octoreview.media`. A delta counter is deferred (TODOS PLT-11).

### 7.7 Cleanup (`manage.py cleanup_media`, CronJob with `concurrencyPolicy: Forbid`)

- Claims rows with `skip_locked`, by setting `cleanup_claimed_until` in its own short transaction,
  then processes each claimed row through the lock-order helper; leased rows are skipped.
- `pending` rows older than `effective_expiry + 10 minutes` move to `deleting`, and their objects
  are deleted.
- Media of `removed` reviews moves to `deleting`. `rejected` and `hidden` reviews keep their objects,
  so a moderator can reverse the decision.
- `deleting` rows are retried until the delete succeeds; then the row becomes `deleted` and
  `review_media.deleted` is emitted.
- Also sweeps local staging files by mtime (older than 1 day). Cloud staging relies on the bucket
  lifecycle rule (7.2).
- Continues past per-object errors and exits 1, listing the failed keys on stderr.

### 7.8 Credentials

- **S3.** Uses the boto3 default credential chain (IRSA or instance role, or access keys).
  Credential expiry is read through one adapter around botocore's credential object, after
  botocore's own refresh. The SigV4 presign cap is 7 days.
- **URL lifetime.** Every signed URL lives `min(configured TTL, backend cap, credential expiry -
  60 s)`. When that lifetime would be under 60 s, the request gets 503
  `storage_credentials_expiring` with `Retry-After`. `upload_expires_at` is computed this way at
  signing time and returned to the SDKs, and a replayed intent's URL is capped the same way.
- **Line 2.** Azure uses `OCTOREVIEW_STORAGE_AZURE_SAS_MODE=account_key|user_delegation`: SAS TTL
  is capped at 10 minutes, and user-delegation keys refresh when less than max(upload TTL, read
  TTL) + 5 minutes remain. GCS uses `OCTOREVIEW_STORAGE_GCS_SIGNING=key_file|iam`: V4 URLs sign
  `x-goog-content-length-range: 0,<size_bytes>`, and `iam` signing runs in a bounded pool of 8 with
  its own latency budget.
- **Size caps.** Azure SAS cannot cap size; on every backend `complete` is the guarantee.
  `docs/storage.md` lists the role or permission each mode needs.

## 8. Read URLs

- **Private storage.** `url` is a presigned GET URL (`OCTOREVIEW_MEDIA_URL_TTL_SECONDS`, default
  900, capped as in 7.8). On the local backend it is `/api/v1/media-files/{token}`.
- **Public-CDN mode.** `OCTOREVIEW_MEDIA_PUBLIC_BASE_URL` makes `url` the base URL plus the final
  key, unsigned. This is the recommended mode for Mbizmarket storefront pages: stable image URLs,
  no per-request signing.
  - Hidden or rejected objects stay reachable by URL until they are deleted.
  - Public-CDN keys expose tenant and application ids.
  - `docs/operations.md` covers CDN purge after erasure, or a short CDN TTL.
- **URL cache** (after rc.1, gate d17b). Signed read URLs are cached in-process, in a bounded LRU
  keyed by the ETag epoch. A URL stays valid at least until the epoch end plus max(TTL/2,
  `max-age`). Until then, reads sign per request, bounded by the list media cap.
- **Signing cost.** A report-only M2 measurement records signing cost per URL.

## 9. Erasure (`POST /authors/erase`, `manage.py erase_author`)

**Request.** `{application_id, author_id}` with `privacy:erase` on an application-wide grant.
Erasure is per application; backups and logs need their own retention (`docs/operations.md`
documents the procedure for UU PDP (UU 27/2022) and GDPR requests).

For each of the author's reviews, in batches of 100 per call and one short transaction per review:
- `title` and `body` become `null`; `author_metadata` and `metadata` become `{"erased": true}`;
  `source_ref` becomes `null`.
- `author_id` becomes `erased:<hmac>`, keyed by the dedicated non-rotating
  `OCTOREVIEW_ERASURE_HMAC_KEY` (boot and `check --deploy` fail without it). This is
  pseudonymisation, and the docs say so.
- The pending revision is deleted, and the review moves to `removed` (aggregates stay correct).
- Its media moves to `deleting`; `cleanup_media` deletes the objects.
- Audit rows: personal fields in `changes`/`metadata` of the author's reviews become
  `{"erased": true}`, and `actor_id` equal to the author becomes `erased:<hmac>`.
- Dead-letter outbox rows about the author are redacted.

Events carry no author data (section 10), so there are no other outbox payloads to rewrite.

**Response:** `{reviews_redacted, pending_media_deletes, remaining}`.

**Re-runs.** Idempotent and re-runnable: re-runs match both the raw and the erased form and finish
leftover media. The command needs `--yes` without a TTY, has `--dry-run`, and exits 1 with a final
re-run line when anything is left.

**Event and audit.** One `author.erased` audit row is written, with counts only. `author.erased` is
the one event that carries the raw `author_id`, with `erased_at`:
- Its outbox row is hard-deleted right after delivery, and an undelivered one is never pruned.
- `docs/events.md` tells consumers to purge their copies and to drop events about that author older
  than `erased_at`.

## 10. Events and webhooks

**Envelope.** Every outbox event has `{id, type, occurred_at, tenant_id, application_id,
namespace_type, namespace_id, resource_type, resource_id, request_id, data}`. Payloads are
**thin**: ids, status, rating, counts and versions, never title, body, `author_metadata`,
`author_id` or `source_ref`. Consumers fetch details over the API.

| Event | `data` |
| --- | --- |
| `review.created`, `review.updated`, `review.revision_submitted` | `review_id`, `entity_id`, `entity_type`, `external_id`, `status`, `rating`, `is_verified_purchase`, `version`, `media_version` |
| `review.status_changed` | the above plus `previous_status` |
| `review.published`, `review.removed` | the same fields as `review.status_changed`; `previous_status` is null for a review published at creation |
| `review_media.ready`, `review_media.invalid`, `review_media.deleted`, `review_media.moderated` | `media_id`, `review_id`, `entity_id`, `kind`, `status`, `moderation_status`, `reason` (invalid only) |
| `entity.rating_changed` | `entity_id`, `entity_type`, `external_id`, `rating_count`, `rating_average`, `histogram`, `verified_review_count` |
| `author.erased` | `author_id` (raw; the one exception), `erased_at`, `reviews_redacted` |

**Emission rules.**
- `review.status_changed` fires on every transition. `review.published` and `review.removed` fire
  in addition when the review enters that status. Erasure is the exception: its `removed` moves
  emit no review events, only `author.erased` (section 9).
- `entity.rating_changed` fires whenever the aggregate changed.
- A namespaced event is delivered like any other, and a test proves it.

**Transports** (`OCTOREVIEW_OUTBOX_TRANSPORT`):
- `logging` (default) logs the event envelope (id, type, resource ids, timestamps) and never
  `payload`.
- `webhook` POSTs the event to `OCTOREVIEW_WEBHOOK_URL` with these headers:
  - `X-Octoreview-Event: <type>`
  - `X-Octoreview-Delivery: <event id>`
  - `X-Octoreview-Signature: t=<unix>,v1=<hex hmac_sha256(OCTOREVIEW_WEBHOOK_SECRET, t + "." + body)>`

  Receivers reject a timestamp outside 300 s. Every SDK ships `webhooks.verify(body, header,
  secret, tolerance = 300 s)` and typed event decoding. Unlike octonomy's body-only signature, this
  signature includes a timestamp (gate d22a).

**Dispatcher** (`manage.py dispatch_outbox_events`, ported from octonomy):
- Claims events, retries with backoff, and moves exhausted events to `dead_letter`. A scheduled
  run delivers pending events and failed ones whose backoff has elapsed.
- `--retry-failed` is an operator replay of failed and dead-letter events, ignoring the backoff;
  it is never scheduled (the manifests run the plain command).
- Dead letters raise an alert (the `outbox.dead_letter_count` metric).

**Pruning** (`manage.py prune_outbox_events`):
- `--published-older-than <days>` (default 30) prunes published events.
- `--dead-letter-older-than <days>` (default 30) prunes dead letters, except an undelivered
  `author.erased`.

## 11. Errors

**Envelope.** Every error, including unexpected 500s, uses
`{"error": {code, message, details, request_id, doc_url, retryable}}`.
- `message` ends with the fix.
- `doc_url` points at `docs/errors.md#<code>`, which has one anchor per code (status, meaning,
  `details` schema, fix, recovery); a test checks it against the codes in `docs/openapi.yaml`.
- SDKs read `requestId` from the body and fall back to the `X-Request-ID` header.

| Status | Code | Details |
| --- | --- | --- |
| 400 | `validation_error` | `fields` |
| 400 | `invalid_namespace` | — |
| 400 | `application_mismatch` | `path`, `body`, `query` |
| 400 | `media_too_large` | `max_bytes`, `kind` |
| 400 | `content_type_not_allowed` | `allowed_content_types` |
| 401 | `authentication_required` | — |
| 401 | `invalid_api_key` | — (the key is never echoed) |
| 403 | `permission_denied` | `reason`, `required_scope`, `grant_scope` |
| 403 | `upload_token_invalid`, `media_token_invalid` | — |
| 404 | `not_found` | `scope` (tenant, application, namespace) |
| 409 | `review_exists` | `review_id` |
| 409 | `entity_inactive` | `entity_id` |
| 409 | `invalid_transition` | `from_status`, `action`, `allowed_actions` |
| 409 | `review_changed` | `expected_version`, `version`, `expected_media_version`, `media_version` |
| 409 | `revision_superseded` | `revision_id` |
| 409 | `revision_stale` | `base_version`, `version` |
| 409 | `source_ref_immutable` | — |
| 409 | `review_not_accepting_media` | `status` |
| 409 | `media_limit_exceeded` | `kind`, `limit`, `ready_count`, `pending_count`, `next_slot_frees_at`, `policy_source` |
| 409 | `storage_quota_exceeded` | `max_storage_bytes`, `stored_bytes`, `reserved_bytes`, `requested_bytes` |
| 409 | `upload_expired` | `upload_expires_at` |
| 409 | `upload_not_found` | — |
| 409 | `upload_changed` | — |
| 409 | `complete_in_progress` | — (`Retry-After`) |
| 409 | `upload_already_completed` | — |
| 410 | `upload_token_expired`, `media_token_expired` | — |
| 411 | `content_length_required` | — |
| 413 | `upload_too_large` | `max_bytes` |
| 416 | `range_not_satisfiable` | — |
| 422 | `media_rejected` | `reason`: `size_mismatch` \| `type_mismatch` \| `over_limit` \| `over_quota` |
| 500 | `internal_error` | — |
| 503 | `storage_unavailable` | `backend`, `operation` (`Retry-After`) |
| 503 | `storage_credentials_expiring` | — (`Retry-After`) |
| 503 | `too_many_completions` | — (`Retry-After`) |

`retryable` is true for 409 `complete_in_progress`, 409 `upload_changed` and every 503.
Errors on the local token paths carry CORS headers.

**Diagnostics.**
- Every `ImproperlyConfigured` message names the variable and ends with the fix command (for
  example the secret generator, `uv sync --extra s3`, or the valid backend list).
- `manage.py check_storage` (`make storage-check`) runs a real PUT to an upload target, then
  `stat`, `read_head`, `finalize`, a GET through `download_url`, and deletes both keys, naming the
  failing step. On a cloud backend the PUT and GET are real HTTP through the signed URLs, and it
  also proves that a missing key answers 404, that a copy of a stale version is refused, and that
  the copy sets the server-chosen content type. It warns when the store accepts an oversize body,
  when the bucket's CORS rule refuses an `OCTOREVIEW_MEDIA_CORS_ORIGINS` origin, and when no
  lifecycle rule expires `_staging/`; it fails on a rule that expires `media/`. The steps are
  listed in [`storage.md`](storage.md).

## 12. Concurrency and transactions

- **Lock order.** One helper takes locks in the order ApplicationSettings → Entity → Review →
  ReviewMedia (ascending id within a type), and tracks ownership in a context variable.
- **Own rows only.** A `select_for_update` whose query joins another table (through
  `select_related` or a filter) passes `of=("self",)`. PostgreSQL's `FOR UPDATE` otherwise locks
  the joined rows too, in one statement, which skips the order above. A `make check` grep gate
  forbids `select_for_update` chained with `select_related` without `of=`.
- **ApplicationSettings.** Locked only by a quota-set intent and a quota-set phase 3. No
  transaction locks ApplicationSettings after a Review.
- **No network under locks.** No network call (signing, stat, read, copy, delete) runs inside a
  transaction that holds a lock. A test asserts that no storage client call happens while the lock
  context variable is set.
- **Short transactions.** Erasure processes one review per short transaction. The review INSERT
  runs in a savepoint.
- **`recompute_* --check`.** Runs in batches by entity-id range, each in its own REPEATABLE READ
  snapshot; the repair locks each entity.
- **Race tests** (PostgreSQL): lock-order deadlock; complete vs complete with a re-PUT between;
  complete vs media DELETE; complete vs erasure; local PUT vs complete; local PUT vs PUT; cleanup vs
  complete; concurrent intents vs quota; intent (quota set) vs complete on the same review. Each
  asserts row states, no storage orphans, and counters equal to a recompute.
- **Clock.** Expiry, TTL and token tests use an injected clock, never sleeps; a `make check` grep
  forbids `time.sleep` in the test tree.

## 13. Server layout and layers

- **Modules.** `config/`, `octoreview/{core,audit,events,service_auth,entities,reviews,media,lifecycle,api,openapi}`,
  with `octoreview.media.storage` for the adapters.
- **Layers.** An import-linter `layers` contract enforces, bottom to top: `core`, `audit` and
  `events`; then `service_auth` → `entities` → `reviews` → `media` → `lifecycle`; then `api`; and
  `openapi` (the contract checks) on top.
  `api` owns views, serializers and urls, and may import every domain module. `lifecycle` owns the
  transition tables, removal cascades and erasure.
- **Encapsulation.** A second contract forbids importing `media.storage` internals outside
  `octoreview.media`.
- **Grep checks in `make check`:**
  - writes to rating columns outside `entities.ratings.apply_delta()`;
  - writes to `ready_media_count` outside the lifecycle service;
  - `NAMESPACE_WRITE_ENABLED`, `NAMESPACE_V2_API_ENABLED` and `api/v2`, which octonomy needed and
    this service drops.
- **Copied code.** Generic pieces (request ids, auth, errors, pagination, responses, service_auth,
  audit, outbox) are copied from octonomy and never imported at runtime. `docs/architecture.md`
  records the octonomy commit each copied module came from.
- **Admin.** The django-unfold admin is copied from octonomy and covers service clients, grants,
  entities, reviews (moderation actions through the domain services) and media. Status, rating and
  counter fields are read-only in it. `OCTOREVIEW_ADMIN_ENABLED` defaults to off in the production
  image and on in compose. The registrations live in `octoreview.console`, a presentation layer
  beside `octoreview.api`. Every model is read-only. Moderation actions (reviews: publish, reject,
  hide, remove; media: approve, reject) call the lifecycle services with an `admin:<username>`
  actor. Publish and approve go through a confirmation page that shows the content and carries
  `expected_version` / `expected_media_version`, so the version guard of 4.4 holds. The site
  admits active superusers only, and `check --deploy` warns (`octoreview.W001`) when it is on with
  DEBUG off. [`operations.md`](operations.md) covers enabling it.

## 14. Management commands

| Command | Purpose |
| --- | --- |
| `create_service_token` | tenant, optional application, `--namespace-type/--namespace-id` or `--namespace-wildcard`, scopes, `--json`; prints the key once; an unknown scope exits 2 listing the valid ones |
| `revoke_service_token` | revoke by prefix |
| `cleanup_media` | 7.7 |
| `erase_author` | 9 |
| `recompute_ratings [--check]` | rebuilds the aggregates and `ready_media_count`; `--check` exits non-zero and lists mismatches without writing |
| `dispatch_outbox_events [--retry-failed]` | 10 |
| `prune_outbox_events` | 10 |
| `check_storage` | 11 |
| `seed` (`make seed`) | demo tenant, application and service key; prints the key once and a working `curl` for `POST /api/v1/reviews` |

The compose stack (`compose.yaml`; `make up`, or `make up-s3` for the `s3` profile with the RustFS
emulator) migrates on start and seeds idempotently when `OCTOREVIEW_DEMO_SEED=1` and DEBUG is on,
and the backend is `local` or an emulator (`seed` refuses a real AWS bucket):
- It writes `.octoreview-dev.env` (`OCTOREVIEW_BASE_URL`, `OCTOREVIEW_API_KEY`,
  `OCTOREVIEW_TENANT_ID`, `OCTOREVIEW_APPLICATION_ID`) into a named volume, and `make dev-env`
  copies it out as the invoking user.
- When that file is gone but the database persists, the seed rotates the demo key.
- The file is git-ignored.

## 15. Configuration

`.env.example` lists every variable with a comment.

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL` | — | as in octonomy |
| `OCTOREVIEW_SERVICE_TOKEN_PEPPER` | required | |
| `OCTOREVIEW_ERASURE_HMAC_KEY` | required | non-rotating |
| `OCTOREVIEW_STORAGE_BACKEND` | `local` | `local` \| `s3` (line 2 adds `azure` \| `gcs`) |
| `OCTOREVIEW_STORAGE_LOCATION_ID` | required | |
| `OCTOREVIEW_PUBLIC_BASE_URL` | required for `local` | |
| `OCTOREVIEW_MEDIA_ROOT` | required for `local` | |
| `OCTOREVIEW_MEDIA_TOKEN_SECRET`, `OCTOREVIEW_MEDIA_TOKEN_SECRET_PREVIOUS` | required for `local` / optional | |
| `OCTOREVIEW_STORAGE_S3_BUCKET`, `OCTOREVIEW_STORAGE_S3_REGION` | required for `s3` | |
| `OCTOREVIEW_STORAGE_S3_ENDPOINT_URL`, `OCTOREVIEW_STORAGE_S3_PUBLIC_ENDPOINT_URL` | unset | the public endpoint presigns host-reachable URLs (the compose S3 emulator); it defaults to the endpoint |
| `OCTOREVIEW_STORAGE_S3_ADDRESSING_STYLE` | `auto` | `auto` \| `path` \| `virtual`; `path` for a store reached by a bare host name |
| `OCTOREVIEW_MEDIA_PUBLIC_BASE_URL` | unset | public-CDN mode |
| `OCTOREVIEW_UPLOAD_URL_TTL_SECONDS` | 900 | |
| `OCTOREVIEW_MEDIA_URL_TTL_SECONDS` | 900 | |
| `OCTOREVIEW_COMPLETE_LEASE_SECONDS` | 900 | |
| `OCTOREVIEW_MAX_CONCURRENT_COMPLETIONS` | 4 | per process |
| `OCTOREVIEW_LIST_MEDIA_LIMIT` | 10 | |
| `OCTOREVIEW_MEDIA_CORS_ORIGINS` | empty | |
| `OCTOREVIEW_MEDIA_CORP` | `cross-origin` | |
| `OCTOREVIEW_DEFAULT_*`, `OCTOREVIEW_*_CEILING` | 6.1 | |
| `OCTOREVIEW_OUTBOX_TRANSPORT` | `logging` | `logging` \| `webhook` |
| `OCTOREVIEW_WEBHOOK_URL`, `OCTOREVIEW_WEBHOOK_SECRET` | required for `webhook` | |
| `OCTOREVIEW_ADMIN_ENABLED` | false in the image, true in compose | |
| `OCTOREVIEW_DEMO_SEED` | 1 in compose | |
| `GUNICORN_WORKER_CLASS`, `GUNICORN_THREADS` | `gthread`, 8 | the other `GUNICORN_*` and `WEB_CONCURRENCY` knobs are in [`deployment.md`](deployment.md) |

## 16. Observability and operations

- **Metrics** (emitted with octonomy's `emit_metric`; [`operations.md`](operations.md) lists each
  with its fields, the alerts and the scheduled jobs):
  - `review.created|published|rejected`;
  - `media.intent|ready|rejected`, with reason and backend;
  - `storage.call`, with backend, operation, latency and outcome;
  - `quota.rejected`, `outbox.lag_seconds`, `outbox.dead_letter_count`;
  - `cleanup_media.deleted|failed`, `erase.completed`.
- **Alerts** (`docs/operations.md`): outbox lag over 10 minutes; storage error rate over 5% for
  5 minutes; cleanup failures for 1 hour; any dead letter. A nightly `recompute_ratings --check`
  CronJob also runs.
- **Logs.** The request log records the resolved route template, never `request.path`. The JSON
  log formatter scrubs `/media-(uploads|files)/<token>` paths and signed query parameters
  (`X-Amz-*`, `sig`, `X-Goog-*`) from every message and exception text, and unmatched routes log
  `"<unresolved>"`. The image ships with gunicorn's access log off. `docs/operations.md` tells
  operators to redact the token paths in proxy and CDN logs.
- **Deployment** ([`deployment.md`](deployment.md)). `deploy/docker` and `deploy/kubernetes` (with CronJobs for `cleanup_media`, the
  outbox dispatcher and the nightly check). Kubernetes requires a cloud storage backend. Rate
  limiting is out of scope: put an API gateway or ingress limit in front (TODOS DEP-3).
- **Smoke.** `scripts/smoke-upload.sh` runs health, create review, intent, PUT, complete and media
  URL fetch against a deployed instance, using a dedicated smoke application. It names the failed
  step and its request id, never prints the key, and deletes what it creates.

## 17. Budgets

These are [estimated, re-measure] values.
- Batch summaries p95 < 50 ms for 100 ids.
- Review list p95 < 150 ms for 20 reviews with media (local signing).
- Intent p95 < 100 ms.
- Complete p95 < 3 s for a 10 MiB image.

Query-count budgets (`django_assert_max_num_queries`) are the CI gate. `scripts/bench.py` (1k
entities, 10k reviews, p50/p95 per endpoint) is report-only and lands after rc.1 (gate d17b).

## 18. SDK contract (Go, PHP, JavaScript)

- **Surface file.** `docs/sdk-surface.yaml` in this repository names every operation, its method
  per SDK, its retry safety, and its required and optional parameters. `docs/sdk-surface.md`, the
  three contract gates and the README retry-safety tables are generated from it.
- **Surface.** Every SDK covers `entities`, `reviews` (including revisions, moderation with the
  version guard, attestation, the author path and moderation-queue filters), `media`, `ratings`
  (including batch and a `ratings.get({entityType, externalId})` helper), `settings`,
  `authors.erase`, `auditLogs`, `health` and `webhooks`.
- **Clients.**
  - `fromEnv()` reads the four `OCTOREVIEW_*` variables.
  - Namespace and `applicationId` defaults are set per client, with per-call overrides (including
    an explicit "global" override) and `includeGlobal` on reads.
  - Every request sends `User-Agent: octoreview-<lang>/<version>`.
  - Read results expose `etag` and accept `ifNoneMatch`.
- **Uploads.** `media.uploadFile(reviewId, path)`, `media.uploadBytes(reviewId, bytes,
  contentType)` and `media.uploadStream(reviewId, stream, contentType, sizeBytes)` run intent →
  PUT → complete and return the ready media.
  - The PUT goes to `upload.url` through a separate HTTP client with only `upload.headers`: never
    `Authorization`, `X-Tenant-ID` or namespace headers. It sets `Content-Length` explicitly,
    disables redirects, and supports bodies above 10 MiB.
  - Kind is inferred from `contentType`; a path infers `contentType` and `sizeBytes`.
  - Failures throw an `UploadError` carrying `stage` (`intent|put|complete`), `mediaId`,
    `reviewId`, `uploadExpiresAt` and request ids, never a signed URL.
  - The PUT is retried up to 3 times with backoff on the same URL before expiry; streams must be
    rewindable. `complete` is retried on 503, `complete_in_progress`, `upload_changed` and timeout.
    A terminal PUT failure best-effort deletes the pending media.
  - Provider XML `<Code>` values are parsed into the error, with a fix.
  - Options: a progress callback, an injectable upload HTTP client, and cancellation (JS
    `AbortSignal`, Go `context`, PHP timeout).
  - `Retry-After` is honored on 429/503, with a configurable bound.
  - Lower-level `createUpload` and `complete` stay public for browser hand-off.
- **Webhooks.** `webhooks.verify` and typed event decoding (section 10).
- **Redaction.** Transport and upload errors are typed and strip query strings and token path
  segments. They never attach raw request config or headers, and never include the API key; a
  per-SDK sentinel test proves it.
- **Languages.**
  - Go: dependency-free `net/http`, functional options, `context.Context` first, `go` directive per
    `docs/versioning.md`.
  - PHP: PHP >= 8.2, one line: Mbizmarket's lowest PHP service runs 8.2 (answered 2026-11-19;
    TODOS PLT-7 closed). PSR-18 with Guzzle by default, immutable request objects.
  - JavaScript: TypeScript strict, axios, dual ESM + CJS, Node >= 20. It refuses browser
    construction unless the caller sets `dangerouslyAllowBrowser: true`.
- **Contract gate.** Each SDK vendors `docs/openapi.yaml`. Its contract gate is offline in
  `make check`, and `.github/workflows/contract-drift.yml` runs it weekly against the latest
  released contract. It:
  - checks every called operation's method, path and parameters, every mapped error code, and the
    response schema ref of every called operation;
  - checks the surface file in both directions;
  - calls every required operation through a recording transport with distinguishable values and
    compares method, path, headers, query and body;
  - decodes response fixtures captured from the container harness (`make fixtures`), asserting
    non-default field values.
- **Quickstart.** Each SDK ships `examples/quickstart`. It reads `.octoreview-dev.env`, creates a
  review with a bundled photo, uploads it, and prints the review id, the rating summary and the
  photo URL. Each SDK's CI runs it.
- **Docs.**
  - Each README states that integrators derive `author_id` from their own authenticated user and
    escape review text on render, and ships one example of each.
  - A compatibility table lives in `docs/versioning.md`.
  - Deprecations emit a language-native warning for at least one minor release.
- **Generation spike.** At most one day spent generating models for one SDK, with the keep/drop
  decision recorded in that SDK's `docs/architecture.md`.

## 19. CI, images and releases

- **Required checks** (the `main: require PR + green CI` ruleset):

  | Repository | Checks |
  | --- | --- |
  | server | `lint`, `checks`, `security`, `test-sqlite`, `test-postgres`, `storage-emulators`, `compose-quickstart`, `docker` |
  | Go SDK | `lint`, `test (oldstable)`, `test (stable)`, `contract`, `vuln`, `integration` |
  | PHP SDK | `static`, `test (8.2)`, `test (8.3)`, `test (8.4)`, `contract`, `integration` |
  | JS SDK | `static`, `test (20)`, `test (22)`, `test (24)`, `contract`, `integration` |

  Placeholder jobs (`octoreview:placeholder`) become real in their feature PRs, and
  `make release-check` fails while any remain.
- **Emulators.** The `storage-emulators` job runs `make test-storage`: the S3 tests and
  `check_storage` against the digest-pinned RustFS image. In line 1 it runs in full on every pull
  request (about a minute). When Azurite and fake-gcs-server join it in line 2, it runs fully when
  storage, media, CI, lockfiles, settings, auth or storage fixtures change, or when path
  classification is uncertain; otherwise it exits green after the path check, and a nightly run
  executes it in full and opens an issue on failure.
- **`docker`.** Builds the image with Buildx (GitHub Actions cache, SHA-pinned actions, no
  registry credential) and runs `make docker-smoke`: non-root, the s3 extra present, no dev tools,
  `manage.py check`, fail-closed startup, live and ready health after `migrate`, `gthread` workers
  and an all-JSON log stream. [`deployment.md`](deployment.md) documents the image.
- **`compose-quickstart`.** Runs `make compose-quickstart` on a clean runner: `docker compose up`
  and the curl quickstart (`scripts/quickstart.sh`: review, photo upload, rating, photo URL) within
  5 minutes, three times (a clean local stack, a second run against the same volumes, a clean
  `s3` profile). The image build time is reported separately, and each run's time goes to the job
  summary.
- **`edge` image.** Server `main` publishes `edge` and `sha-<commit>` images (`publish-image.yml`;
  tags in [`versioning.md`](versioning.md)) on `workflow_run` of CI,
  only when `workflow_run.event == 'push'`, `head_branch == 'main'`,
  `head_repository.full_name == github.repository` and `conclusion == 'success'`. It builds
  `workflow_run.head_sha` after the same ancestry check that octonomy's `publish-image.yml` runs.
- **SDK CI.** SDK PR CI pins a `sha-<commit>` server image named in `docs/versioning.md` and bumped
  by a bot PR. A scheduled SDK job runs against `edge` and opens an issue on failure. Each SDK's
  upload integration test also runs against the compose `s3` profile (the S3 emulator). The "previous SDK
  against next server" job skips until a previous SDK release exists.
- **Release candidates.** rc tags (`vX.Y.Z-rc.N`) publish immutable image tags without moving
  `latest` or stable aliases. `contract-drift.yml` and `resolve-latest-tag` treat the newest rc as
  latest until `1.0.0`.
- **Release smoke.** Before each tag, a smoke run of the upload flow (including the browser
  hand-off) uses one real bucket per provider and each documented signing mode. A backend stays
  "beta" until its smoke run passes once.
- **Publishing.**
  - npm publishes from CI through npm trusted publishing (OIDC, `--provenance`, no long-lived
    token).
  - Go: the tag is the release, and the runbook verifies it through `proxy.golang.org`.
  - PHP: Packagist, through its GitHub webhook.
- **Migrations.** Migrations are never edited after `v1.0.0-rc.1`, and an upgrade test migrates
  rc.N data to rc.N+1.
- **Contract freeze.** `docs/openapi.yaml`, `docs/events.md` and `docs/errors.md` merge before SDK
  work depends on them (`make openapi-check`).

## 20. Not in scope for line 1

Video transcoding, thumbnails and resizing; automatic content moderation; end-user
authentication; GraphQL; per-tenant storage backends (PLT-2); helpfulness votes (PRD-1); merchant
replies (PRD-2, which waits for Mbizmarket to ask); multi-criteria ratings (PRD-3); weighted scores
(PRD-4); an MCP server (PLT-1); systemd units (DEP-1); a shared `octoverse-core` (PLT-3);
write-once uploads (PLT-10); a quota delta counter (PLT-11); audit retention (PLT-12); a
`reviews:read_unpublished` scope (PLT-13); rate limiting (DEP-3); a Python SDK (PLT-6); the
attempt-key fallback for stores without conditional copy (PLT-14). See
[`TODOS.md`](../TODOS.md).

## 21. Contradiction table

| # | Item | Superseded values (source) | Value that holds (source) |
| --- | --- | --- | --- |
| 1 | Inbound request-id length | `{1,128}` (CEO "Request id"; Eng N13 cycle 2) | `{1,100}`, to fit the copied 100-character columns (DX "Request-id width"; Eng cycle 3) |
| 2 | Media status after a failed `complete` | `rejected` (CEO "Media lifecycle") | `invalid` (DX consistency fixes) |
| 3 | Event for a failed `complete` | `review_media.rejected` (CEO) | `review_media.invalid`; moderation emits `review_media.moderated` (DX) |
| 4 | Media statuses | `pending|ready|invalid|deleted` (DX) | adds `deleting` (Eng R3) |
| 5 | Final key layout | `{tenant}/{application}/{entity_type}/{entity_id}/...` (base), `{tenant}/{application}/{entity_id}/...` (CEO) | `media/{tenant}/{application}/{entity_id}/{review_id}/{media_id}.{ext}` (Eng R5, N4) |
| 6 | Staging key | `{tenant}/{application}/_staging/{media_id}` (CEO) | `_staging/{tenant}/{application}/{media_id}` (Eng A5) |
| 7 | Concurrent completes | per-attempt final keys and persisted copy state (Eng R1) | one deterministic key with a conditional copy; attempt keys only on a backend without conditional copy (Eng N4) |
| 8 | Validation order | copy to the final key, then validate it (Eng A1) | validate staging, then conditionally copy the validated version (Eng N4) |
| 9 | Long copies | 503 `storage_copy_in_progress` after 20 s (Eng A1); multipart `UploadPartCopy` above 64 MiB (Eng N15 cycle 2) | one `CopyObject` inside a 900 s lease, no copy deadline for line-1 backends (Eng N15 cycle 3) |
| 10 | Idempotent creates | `Idempotency-Key` on `POST /reviews` and media intents (base) | none; duplicate reviews get 409 `review_exists`; intents retry with `client_upload_id` (CEO, Eng R18) |
| 11 | Stored bytes | a `stored_bytes` counter updated with `F()` and a single writer (CEO, Eng A3/A4, R13) | computed `SUM` when a quota is set; no counter, no recompute command (Eng N2, gate d19a) |
| 12 | Reserved bytes | a `reserved_bytes` counter (CEO) | computed `SUM` (Eng A3) |
| 13 | Upload expiry | 409 `upload_expired` right after `upload_expires_at` (CEO); 5-minute grace for complete only (Eng R21) | one `effective_expiry = upload_expires_at + 5 min` for slots, reservations, `next_slot_frees_at`, `upload_expired` and cleanup (Eng N9) |
| 14 | Slot count | `ready` + unexpired `pending` (CEO) | `ready` + `pending` within `effective_expiry` or holding a live lease (Eng N9, cycle 3) |
| 15 | List with unsignable URLs | 503 for the whole list (DX) | `url: null` + `url_unavailable: "signing"` (Eng R15, gate d18a) |
| 16 | Local token checks | every use re-checks the row (Eng S1); re-check only revocation (Eng R16) | per-audience revocation (Eng C4) |
| 17 | Author path | `author_id` filter alone shows any status (CEO) | needs `include_unpublished=true` (Eng R24-R27) |
| 18 | Batch summaries caching | ETag and 304 (CEO) | `Cache-Control` only, no validators (Eng E5) |
| 19 | ETag input | hash(data version, epoch) (Eng E5); `(id, version, updated_at)` (Eng R20) | ordered `(id, version, media_version)` + total count + epoch (Eng N6) |
| 20 | ETag, URL cache and bench timing | before `v1.0.0-rc.1` | after rc.1, before `v1.0.0` (gate d17b) |
| 21 | Cross-Origin-Resource-Policy | `same-site` (Eng S6) | configurable, default `cross-origin` (Eng R17) |
| 22 | SDK upload entry points | `media.upload(reviewId, file\|stream\|bytes)` (base), `media.upload(reviewId, path)` (DX) | `media.uploadFile` / `uploadBytes` / `uploadStream` (Eng H3) |
| 23 | SDK integration jobs | skip until a published rc (Eng H2); run against `edge` (Eng R8) | PR CI pins a `sha-<commit>` image; a scheduled job runs `edge` (Eng N20) |
| 24 | Compose quickstart check | Go quickstart (DX); JS quickstart (gate rev 1 for Octocart) | server CI runs the curl quickstart; each SDK's CI runs its own (owner correction, Mbizmarket) |
| 25 | Erasure scope | within the caller's grant (Eng erasure inventory) | an application-wide grant is required (Eng N8) |
| 26 | Erasure of outbox payloads | rewrite payloads and re-check `redacted_at` before sending (CEO, Eng) | not needed: events are thin; dead letters are redacted (Eng N3, N14) |
| 27 | Event payloads | full review content (CEO, DX) | thin: ids, status, rating, counts, versions; never author data except `author.erased` (Eng N3, gate d20a) |
| 28 | Logging transport | logs the whole serialized event (octonomy) | logs the envelope only (Eng N3) |
| 29 | Audit columns | `aggregate_type`, `aggregate_id`, `before`, `after`, "same shape as octonomy" (base) | `aggregate_type`, `aggregate_id`, `changes`, `metadata` (Eng N14) |
| 30 | Outbox pruning | `--delivered-older-than`, index `(status, delivered_at)` (CEO, Eng A7) | `--published-older-than`, index `(status, published_at)` (Eng N14) |
| 31 | Dead letters | no rule | alert, `--retry-failed` replay, prune after 30 days except an undelivered `author.erased` (Eng N14) |
| 32 | npm publishing | from the release runbook (CEO) | from CI with npm trusted publishing (Eng R27, N19) |
| 33 | JS SDK in a browser | refuses when `window` is defined (Eng R25) | refuses unless `dangerouslyAllowBrowser: true` (Eng N18) |
| 34 | Admin | always on (CEO) | `OCTOREVIEW_ADMIN_ENABLED`, off in the production image (Eng R11, C7) |
| 35 | Setting `source_ref` | on create and update with `reviews:attest` (Eng R6) | on create only (Eng C2) |
| 36 | `version` bumps | every applied content change (Eng E3) | applied rating, title or body changes only; media changes bump `media_version` (Eng N1, C3) |
| 37 | Moderation conflict codes | `revision_superseded` (DX), `revision_stale` (Eng E3), `review_changed` (Eng N1) | all three, with precedence superseded → changed → stale (Eng C3) |
| 38 | Where storage usage lives | `octoreview.entities` "storage counters" (Eng app structure) | `octoreview.media` (Eng C1) |
| 39 | Layers | `core → service_auth → entities → reviews → media → lifecycle` (Eng R9) | `core`/`audit`/`events` at the bottom, `api` on top (Eng N7, C1) |
| 40 | Latency bench gate | `make release-check` fails at +50% (Eng Section 4) | report-only; query counts gate (Eng R31) |
| 41 | Gunicorn | `sync`, 1 thread (octonomy) | `gthread`, `GUNICORN_THREADS` 8 (Eng N15) |
| 42 | Inactive entity aggregates | "frozen" (CEO) | kept current by moderation; "frozen" means no new contributions (DX consistency fixes) |
| 43 | Media URL lifetime cap | Azure only (Eng E6) | every backend: `min(TTL, backend cap, credential expiry - 60 s)` (Eng R14, N5) |
| 44 | Dependabot ecosystems at bootstrap | uv, gomod, composer, npm (plan) | GitHub Actions only until each repository's first manifest lands (bootstrap, Dependabot errors on an ecosystem with no manifest) |
| 45 | S3 emulator (CI and the compose `s3` profile) | MinIO (plan, Eng T1) | RustFS 1.0.6, pinned by digest: MinIO's images left Docker Hub and Quay in September 2026, and RustFS passed every spike check (week-1 spike, [`storage.md`](storage.md)) |
| 46 | Stores without conditional copy | the attempt-key fallback (Eng N4) | not built in line 1; `check_storage` refuses such a store (week-1 spike; TODOS PLT-14) |
| 47 | `storage-emulators` path filter and nightly run | from the first emulator (plan) | full run on every pull request in line 1; the filter arrives with the line-2 emulators |

## 22. Spec decisions

These are values the plan left open; this file settles them.

| Item | Decision |
| --- | --- |
| Service key prefix | `ocr_<8 hex>_<secret>`, with a 200-character maximum (octonomy uses `octo`) |
| Pepper variable | `OCTOREVIEW_SERVICE_TOKEN_PEPPER` |
| Intent on a hidden, rejected or removed review | 409 `review_not_accepting_media` |
| Quota exceeded at phase 3 | 422 `media_rejected` with reason `over_quota` |
| Completion cap exceeded | 503 `too_many_completions` |
| Local token errors | 403 `*_token_invalid`, 410 `*_token_expired`, 409 `upload_already_completed` |
| Half-set namespace headers | 400 `invalid_namespace` |
| Path/body/query application disagreement | 400 `application_mismatch` |
| Erased review fields | `title`/`body` → `null`; `author_metadata`/`metadata` → `{"erased": true}`; `source_ref` → `null` |
| Event envelope and `data` fields | section 10 |
| Webhook headers | `X-Octoreview-Event`, `X-Octoreview-Delivery`, `X-Octoreview-Signature` |
| `ApplicationSettings.moderation_mode` null | means `auto_publish` |
| Initial `version` and `media_version` | 1 |
| Embedded media cap | `OCTOREVIEW_LIST_MEDIA_LIMIT` applies to review details as well as list items (the plan named lists); `GET /reviews/{id}/media` returns the full set |
