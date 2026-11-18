# Error codes

Every error response uses one envelope (spec section 11):

```json
{
  "error": {
    "code": "media_limit_exceeded",
    "message": "This review already has its 5 image slot(s) in use; delete one or wait for an abandoned upload to expire.",
    "details": {"kind": "image", "limit": 5, "ready_count": 4, "pending_count": 1, "next_slot_frees_at": "2026-11-19T10:15:00+00:00", "policy_source": "entity"},
    "request_id": "req_6f1c0b0e2f4d4a3c9a1b2c3d4e5f6a7b",
    "doc_url": "https://github.com/octoverse-id/octoreview/blob/main/docs/errors.md#media_limit_exceeded",
    "retryable": false
  }
}
```

- Branch on `code`, never on `message`. Messages end with the fix and may be reworded.
- `retryable: true` means the same request may succeed later. Honor `Retry-After` when present.
- `request_id` is also in the `X-Request-ID` header; quote it when you report a problem.
- This file has one section per code the server sends. `make openapi-check` fails when a code is
  missing here, missing from `docs/openapi.yaml`, or has a different status or retry flag.

## 400 Bad Request

### validation_error

**Status:** 400 · **Retryable:** no

**Meaning:** The request body or query failed validation.

**Details:** `fields`: an object mapping each field name to a list of messages.

**Fix:** Correct the listed fields and send the request again.

**Recovery:** Not retryable as is; the same request fails the same way.

### invalid_namespace

**Status:** 400 · **Retryable:** no

**Meaning:** `X-Namespace-Type` and `X-Namespace-ID` were not sent together, were longer than 100
characters, or used the reserved type `global`.

**Details:** none.

**Fix:** Send both namespace headers or neither.

**Recovery:** Not retryable as is.

### application_mismatch

**Status:** 400 · **Retryable:** no

**Meaning:** The path, query and body named different `application_id` values.

**Details:** `path`, `body`, `query`: the values that were sent.

**Fix:** Send one `application_id`, or the same value everywhere.

**Recovery:** Not retryable as is.

### media_too_large

**Status:** 400 · **Retryable:** no

**Meaning:** The intent's `size_bytes` is above the kind's maximum for this entity.

**Details:** `max_bytes`, `kind`.

**Fix:** Compress or trim the file below `max_bytes`, or raise the policy (entity, then
application settings).

**Recovery:** Not retryable as is.

### content_type_not_allowed

**Status:** 400 · **Retryable:** no

**Meaning:** The content type is not one of the allowed types for this entity.

**Details:** `allowed_content_types`.

**Fix:** Convert the file to an allowed type (for example HEIC to JPEG) and send a new intent.

**Recovery:** Not retryable as is.

## 401 Unauthorized

### authentication_required

**Status:** 401 · **Retryable:** no

**Meaning:** No `Authorization: Bearer <key>` header, or a malformed one.

**Details:** none.

**Fix:** Send the service key as `Authorization: Bearer ocr_...`.

**Recovery:** Not retryable as is.

### invalid_api_key

**Status:** 401 · **Retryable:** no

**Meaning:** The key is unknown, revoked or expired. The key is never echoed.

**Details:** none.

**Fix:** Use a current key; create one with `manage.py create_service_token`.

**Recovery:** Not retryable as is.

## 403 Forbidden

### permission_denied

**Status:** 403 · **Retryable:** no

**Meaning:** The key has no grant for this tenant, application, namespace and scope together.

**Details:** `reason`, one of `scope_missing`, `tenant_not_granted`, `application_not_granted`,
`application_id_required`, `application_required_for_namespace`, `namespace_not_granted`,
`application_wide_grant_required`, `endpoint_has_no_scope`; plus `required_scope` and
`grant_scope` when they apply.

**Fix:** Use a key whose grant covers the scope named in `required_scope`, or send the missing
`application_id`.

**Recovery:** Not retryable as is.

### upload_token_invalid

**Status:** 403 · **Retryable:** no

**Meaning:** The local upload URL is not valid: altered, signed with an unknown secret, or for a
media item that no longer exists.

**Details:** none.

**Fix:** Request a new intent and PUT to its `upload.url` unchanged.

**Recovery:** Not retryable with this URL.

### media_token_invalid

**Status:** 403 · **Retryable:** no

**Meaning:** The local media URL is not valid.

**Details:** none.

**Fix:** Read the review or media again for a fresh `url`.

**Recovery:** Not retryable with this URL.

## 404 Not Found

### not_found

**Status:** 404 · **Retryable:** no

**Meaning:** The resource does not exist, or exists outside the key's tenant, application or
namespace. A revoked or invisible media URL also answers 404.

**Details:** `scope`: the `tenant_id`, `application_id`, `namespace_type` and `namespace_id` the
lookup searched (null when not sent). Absent on the local media URL paths.

**Fix:** Check the id and the tenant, application and namespace headers; send
`include_global=true` to read global rows from a namespaced request.

**Recovery:** Not retryable as is.

## 405, 406 and 415

### method_not_allowed

**Status:** 405 · **Retryable:** no

**Meaning:** The path does not accept this HTTP method.

**Details:** none.

**Fix:** Use a method `docs/openapi.yaml` lists for the path.

**Recovery:** Not retryable as is.

### not_acceptable

**Status:** 406 · **Retryable:** no

**Meaning:** The `Accept` header asks for a format the API does not produce.

**Details:** none.

**Fix:** Send `Accept: application/json`, or omit the header.

**Recovery:** Not retryable as is.

### unsupported_media_type

**Status:** 415 · **Retryable:** no

**Meaning:** The request body is not JSON.

**Details:** none.

**Fix:** Send `Content-Type: application/json` with a JSON body.

**Recovery:** Not retryable as is.

## 409 Conflict

### review_exists

**Status:** 409 · **Retryable:** no

**Meaning:** This author already has an active review of this entity (for this `source_ref`, when
one is sent).

**Details:** `review_id`: the existing review.

**Fix:** Edit the existing review with `PATCH /reviews/{review_id}` instead.

**Recovery:** After a lost response to your own create, this is your review: read it by id.

### entity_inactive

**Status:** 409 · **Retryable:** no

**Meaning:** The entity is deactivated, so it takes no new reviews, edits or media.

**Details:** `entity_id`.

**Fix:** Reactivate it with `PATCH /entities/{entity_id}` and `{"is_active": true}` if that is
intended.

**Recovery:** Not retryable until the entity is active.

### invalid_transition

**Status:** 409 · **Retryable:** no

**Meaning:** The review's status does not allow this action (for example hiding a pending review,
or deleting media of a removed review).

**Details:** `from_status`, `action`, `allowed_actions`.

**Fix:** Pick one of `allowed_actions`, or read the review to see its current status.

**Recovery:** Not retryable as is. After a lost response, the earlier attempt may have succeeded:
read the review.

### review_changed

**Status:** 409 · **Retryable:** no

**Meaning:** The review or its media changed after the moderator loaded it.

**Details:** `expected_version`, `version`, `expected_media_version`, `media_version`.

**Fix:** Reload the review, look at it again, and resend with the current versions.

**Recovery:** Retry only after a human decision on the current content.

### revision_superseded

**Status:** 409 · **Retryable:** no

**Meaning:** A newer edit replaced the revision you acted on.

**Details:** `revision_id`: the current revision.

**Fix:** Load `GET /reviews/{review_id}/revision` and decide on the current revision.

**Recovery:** Not retryable as is.

### revision_stale

**Status:** 409 · **Retryable:** no

**Meaning:** The review changed since the revision was submitted, so applying it would overwrite
newer content.

**Details:** `base_version`, `version`.

**Fix:** Reject the revision and let the author edit the current review.

**Recovery:** Not retryable as is.

### source_ref_immutable

**Status:** 409 · **Retryable:** no

**Meaning:** `source_ref` is set only when the review is created.

**Details:** none.

**Fix:** Create a new review for the other order instead of editing `source_ref`.

**Recovery:** Not retryable as is.

### review_not_accepting_media

**Status:** 409 · **Retryable:** no

**Meaning:** Only `pending` and `published` reviews accept media.

**Details:** `status`.

**Fix:** Upload media while the review is pending or published.

**Recovery:** Not retryable as is.

### media_limit_exceeded

**Status:** 409 · **Retryable:** no

**Meaning:** Every image or video slot of the review is used by ready media or live uploads.

**Details:** `kind`, `limit`, `ready_count`, `pending_count`, `next_slot_frees_at` (when an
abandoned upload frees its slot, or null), `policy_source` (`entity`, `application` or `server`).

**Fix:** Delete a media item, or wait until `next_slot_frees_at`. Raise the limit where
`policy_source` says it comes from.

**Recovery:** Retry after a slot frees.

### storage_quota_exceeded

**Status:** 409 · **Retryable:** no

**Meaning:** The application's `max_storage_bytes` would be exceeded.

**Details:** `max_storage_bytes`, `stored_bytes`, `reserved_bytes`, `requested_bytes`.

**Fix:** Delete media, or raise `max_storage_bytes` in the application settings.

**Recovery:** Retry after space frees; reserved bytes free when abandoned uploads expire.

### upload_expired

**Status:** 409 · **Retryable:** no

**Meaning:** `complete` came after the upload URL expired and its grace period ended.

**Details:** `upload_expires_at`.

**Fix:** Create a new intent and upload again.

**Recovery:** Not retryable for this media id.

### upload_not_found

**Status:** 409 · **Retryable:** no

**Meaning:** `complete` found no uploaded file; the media row stays pending.

**Details:** none.

**Fix:** PUT the file to `upload.url`, then call `complete` again.

**Recovery:** Retry after the PUT succeeds.

### upload_changed

**Status:** 409 · **Retryable:** yes

**Meaning:** The file was uploaded again while `complete` was validating it.

**Details:** none.

**Fix:** Call `complete` again.

**Recovery:** Retry; the next `complete` validates the newest upload.

### complete_in_progress

**Status:** 409 · **Retryable:** yes

**Meaning:** Another `complete` call for this media is running (for example the first attempt
after a proxy timeout).

**Details:** none; `Retry-After` gives the seconds until its lease ends.

**Fix:** Wait for `Retry-After`, then call `complete` again; it returns the ready media once the
other call finished.

**Recovery:** Retry after `Retry-After`.

### upload_already_completed

**Status:** 409 · **Retryable:** no

**Meaning:** The local upload URL belongs to media that is no longer pending.

**Details:** none.

**Fix:** Create a new intent to upload another file.

**Recovery:** Not retryable with this URL.

## 410, 411, 413 and 416

### upload_token_expired

**Status:** 410 · **Retryable:** no

**Meaning:** The local upload URL expired.

**Details:** none.

**Fix:** Retry the intent with the same `client_upload_id` for a fresh URL, or create a new intent.

**Recovery:** Not retryable with this URL.

### media_token_expired

**Status:** 410 · **Retryable:** no

**Meaning:** The local media URL expired.

**Details:** none.

**Fix:** Read the review or media again for a fresh `url`.

**Recovery:** Not retryable with this URL.

### content_length_required

**Status:** 411 · **Retryable:** no

**Meaning:** The upload PUT had no `Content-Length`; chunked uploads are not accepted.

**Details:** none.

**Fix:** Send `Content-Length` with the exact size (the SDKs always do).

**Recovery:** Retry with the header set.

### upload_too_large

**Status:** 413 · **Retryable:** no

**Meaning:** The upload body is larger than the intent's `size_bytes`.

**Details:** `max_bytes`: the declared size.

**Fix:** Upload exactly the file you declared, or create a new intent with the right size.

**Recovery:** Not retryable with this body.

### range_not_satisfiable

**Status:** 416 · **Retryable:** no

**Meaning:** The `Range` header asks for bytes outside the file (`bytes=-0`, a reversed range or a
start beyond the end). `Content-Range: bytes */<size>` gives the size.

**Details:** none.

**Fix:** Request a range inside the file, or omit `Range`.

**Recovery:** Not retryable as is.

## 422 Unprocessable

### media_rejected

**Status:** 422 · **Retryable:** no

**Meaning:** `complete` refused the uploaded file; the media row is now `invalid` and its slot is
free.

**Details:** `reason`: `size_mismatch` (the file is not `size_bytes` long), `type_mismatch` (the
bytes are not the declared `content_type`), `over_limit` (the slots filled meanwhile) or
`over_quota` (the storage quota filled meanwhile).

**Fix:** For a mismatch, upload the right file with a new intent. For a limit or quota, free space
first.

**Recovery:** Not retryable for this media id.

## 500 and 503

### internal_error

**Status:** 500 · **Retryable:** no

**Meaning:** An unexpected server error. Details stay in the server log.

**Details:** none.

**Fix:** Report the `request_id` to the operator.

**Recovery:** A read may be retried once; check the state before retrying a write.

### storage_unavailable

**Status:** 503 · **Retryable:** yes

**Meaning:** The storage backend failed or is nearly full.

**Details:** `backend`, `operation`; `Retry-After`.

**Fix:** Retry after `Retry-After`. Operators: run `make storage-check`.

**Recovery:** Retry after `Retry-After`.

### storage_credentials_expiring

**Status:** 503 · **Retryable:** yes

**Meaning:** The server's storage credentials expire before a signed URL could be used.

**Details:** none; `Retry-After`.

**Fix:** Retry after `Retry-After`, once the credentials have refreshed.

**Recovery:** Retry after `Retry-After`.

### too_many_completions

**Status:** 503 · **Retryable:** yes

**Meaning:** This server process is already validating its maximum number of uploads.

**Details:** none; `Retry-After`.

**Fix:** Retry after `Retry-After`.

**Recovery:** Retry after `Retry-After`.
