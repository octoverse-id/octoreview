# Operations

Running octoreview day to day: what it reports, what to alert on, the scheduled jobs, and what
to do when something fails. [`spec.md`](spec.md) section 16 is the authority;
[`deployment.md`](deployment.md) covers the image and [`storage.md`](storage.md) the bucket.

## Logs and metrics

Every log line is one JSON object on stderr. There is no metrics endpoint: a metric is a log line
with a `metric` field, and your log pipeline counts and aggregates it. For example:

```json
{"level": "INFO", "logger": "octoreview.metrics", "message": "storage.call", "metric": "storage.call", "backend": "s3", "operation": "finalize", "latency_ms": 41.3, "outcome": "ok"}
```

Metric fields carry scope ids, kinds, reasons and counts, never personal data (no author ids,
titles or bodies). Request logs record the route template and the request id, never the raw path.

| Metric | Fields | Emitted |
| --- | --- | --- |
| `review.created` | `tenant_id`, `application_id` | a review was created (after commit) |
| `review.published` | same | a review became public, at creation or by moderation |
| `review.rejected` | same | a moderator rejected a review |
| `media.intent` | scope, `kind`, `backend`, `size_bytes` | an upload intent created a row (not on a replay) |
| `media.ready` | same | `complete` published a media item |
| `media.rejected` | scope, `kind`, `backend`, `reason` | `complete` rejected the bytes: `size_mismatch`, `type_mismatch`, `type_not_allowed`, `too_large`, `over_limit` or `over_quota` |
| `quota.rejected` | scope, `kind`, `backend`, `stage` | the storage quota refused an upload, at `intent` (409) or `complete` (422) |
| `storage.call` | `backend`, `operation`, `latency_ms`, `outcome` | every storage protocol call: `sign_upload`, `stat`, `read_head`, `finalize`, `delete`, `sweep_staging`, `lifecycle`. Read-URL signing is not counted (a list signs one URL per media item). |
| `cleanup_media.deleted` | `count`, `swept`, `backend` | each `cleanup_media` run |
| `cleanup_media.failed` | `count`, `backend` | each `cleanup_media` run |
| `erase.completed` | scope, `reviews_redacted`, `pending_media_deletes` | an author erasure finished its last batch |
| `outbox.lag_seconds` | `seconds`, `backlog` | each dispatcher run: the age of the oldest deliverable event |
| `outbox.dead_letter_count` | `count` | each dispatcher run |
| `outbox_dispatch_summary` | per-run counts, `backlog`, `lag_seconds` | each dispatcher run |

`storage.call` outcomes: `ok`; `missing` (a `stat` found nothing, as when a client completes
before uploading); `precondition_failed` (the upload changed during `complete`); `final_exists`
(an earlier attempt already published the bytes); `credentials_expiring`; and `error`. Only
`error` is a fault. The others are answers the protocol expects.

## Smoke test

`scripts/smoke-upload.sh` checks a deployed instance end to end: health, create a review,
upload a photo through intent, PUT and complete, then fetch its URL and compare the bytes. It
deletes the media item and the review it created, names the failed step and its request id, and
never prints the key. Give it a key for a dedicated smoke application (scopes `reviews:read`,
`reviews:write`, `media:write`):

```sh
OCTOREVIEW_BASE_URL=https://reviews.example.com OCTOREVIEW_API_KEY=... \
OCTOREVIEW_TENANT_ID=example OCTOREVIEW_SMOKE_APPLICATION_ID=smoke scripts/smoke-upload.sh
```

Run it after each deployment and from your monitoring on a schedule. A failed `upload_put` or
`media_fetch` points at the bucket (CORS, credentials, a presign host the network cannot reach);
`check_storage` from a pod narrows it down. The CI `compose-quickstart` job runs it against the
compose stack on both backends.

## Alerts

| Alert | Condition | First look |
| --- | --- | --- |
| Outbox lag | `outbox.lag_seconds` `seconds` above 600 | Is the dispatcher running? Is the webhook receiver answering? |
| Storage errors | `storage.call` with `outcome` `error` above 5% of all `storage.call` for 5 minutes | `manage.py check_storage` names the failing step; see below |
| Cleanup failing | `cleanup_media.failed` `count` above 0 on every run for 1 hour | The command lists the failed keys on stderr and exits 1 |
| Dead letters | `outbox.dead_letter_count` `count` above 0 | Fix the receiver, then replay once by hand: `dispatch_outbox_events --retry-failed` (it ignores the backoff and gives dead letters a fresh retry budget) |

## Scheduled jobs

`deploy/kubernetes/` ships each as a CronJob with `concurrencyPolicy: Forbid`, and
`deploy/docker/compose.yaml` runs the dispatcher and cleanup as loops. Elsewhere, schedule them
with the same image and environment as the server:

| Command | Schedule | Why |
| --- | --- | --- |
| `python manage.py dispatch_outbox_events` | every minute | delivers events (webhook transport) and retries failed ones after their backoff; never schedule `--retry-failed`, which replays dead letters |
| `python manage.py cleanup_media` | every 15 minutes | deletes abandoned uploads and the media of removed reviews; exits 1 on a failed key |
| `python manage.py prune_outbox_events` | daily | drops delivered and old dead-letter events (`--published-older-than`, `--dead-letter-older-than`) |
| `python manage.py recompute_ratings --check` | nightly | exits non-zero and lists rating or media-count mismatches; writes nothing |

## When something fails

- **503 `storage_unavailable`** (`details.backend`, `details.operation`). The bucket or the disk
  is failing. Run `manage.py check_storage` from a server pod: it names the step, and for S3 the
  likely missing permission. Clients retry after `Retry-After`. Readiness never calls storage, so
  pods stay in rotation and reviews without media keep working.
- **503 `storage_credentials_expiring`.** The role credentials could not be refreshed (STS or
  IRSA trouble). URLs never outlive their credential, so the server refuses to sign until it
  can. Check the pod's service account and STS reachability.
- **503 `too_many_completions`.** More than `OCTOREVIEW_MAX_CONCURRENT_COMPLETIONS` completions
  are copying in one process. Clients retry; scale out, or raise the cap together with
  `GUNICORN_THREADS`.
- **409 `complete_in_progress` after a proxy 504.** The first `complete` is still copying inside
  its lease; see the idle-timeout note in [`deployment.md`](deployment.md).
- **`recompute_ratings --check` reports a mismatch.** Run `recompute_ratings` without `--check`
  to rebuild the aggregates, then look for a code path that wrote around the lifecycle service.

## Privacy and retention

- **Token paths in logs.** `/api/v1/media-uploads/<token>` and `/api/v1/media-files/<token>`
  carry bearer tokens, and presigned URLs carry signatures in their query strings (`X-Amz-*`).
  The server scrubs both from its own logs; redact them in proxy, load-balancer and CDN logs as
  well.
- **Erasure and copies.** `POST /authors/erase` (or `manage.py erase_author`) redacts the
  author's reviews and marks their media for deletion, per application (`cleanup_media` deletes
  the objects), and emits `author.erased` so webhook consumers purge their copies. It cannot reach copies outside the database and bucket:
  - **CDN.** In public-CDN mode (`OCTOREVIEW_MEDIA_PUBLIC_BASE_URL`), purge the erased media's
    URLs, or keep the CDN TTL short.
  - **Backups.** Database and bucket backups keep erased data until they expire; give them a
    retention you can state in your privacy policy.
  - **Logs.** Request logs hold no review content, but your pipeline's retention still applies.
  - **Bucket versioning.** Keep it off, or expire noncurrent versions ([`storage.md`](storage.md)).

## Admin console

An optional operator console at `/admin/` (django-unfold), for looking things up and moderating
by hand. The REST API stays the product surface.

- **Turning it on.** Set `OCTOREVIEW_ADMIN_ENABLED=true` (off in the image, on in compose). When
  off, `/admin/` does not exist. Create a login with
  `python manage.py createsuperuser` (in compose:
  `docker compose exec octoreview python manage.py createsuperuser`).
- **Who gets in.** Active superusers only; a staff account without superuser cannot enter.
- **What it does.**
  - Every model is read-only: service keys (the key hash never shows), grants, entities,
    application settings, reviews, revisions, media, audit logs and outbox events.
  - Moderation actions call the same lifecycle services as the API, recorded with the actor
    `admin:<username>`. Reviews can be rejected, hidden or removed. Publishing a review or
    approving a media item first shows a confirmation page with the content (and a link to the
    file); a review that changed after that page rendered is refused, and you reload it.
- **In production.** Serve it over HTTPS to trusted operators only (a VPN or an IP allowlist on
  `/admin/`). `manage.py check --deploy` warns (`octoreview.W001`) while it is on with DEBUG off.
  Its static files are collected into the image and served by the app.

## Rotating secrets

| Secret | How |
| --- | --- |
| `OCTOREVIEW_MEDIA_TOKEN_SECRET` (local backend) | Move the current value to `OCTOREVIEW_MEDIA_TOKEN_SECRET_PREVIOUS`, set a new one, deploy. Tokens name the secret that signed them, so URLs issued before the rotation keep working; drop the previous value once the longest URL lifetime (`OCTOREVIEW_UPLOAD_URL_TTL_SECONDS`, `OCTOREVIEW_MEDIA_URL_TTL_SECONDS`) has passed |
| Service keys | `create_service_token` a new key, switch the caller, then `revoke_service_token` the old prefix |
| `DJANGO_SECRET_KEY` | Replace and deploy; it signs only admin console sessions, so console users log in again |
| `OCTOREVIEW_SERVICE_TOKEN_PEPPER` | A cutover with downtime: stored key hashes depend on the pepper, so every key, old or new, stops working when it changes, and keys created before the switch are hashed with the old pepper. Create each caller's replacement key with the new pepper in that command's environment (`OCTOREVIEW_SERVICE_TOKEN_PEPPER=<new> python manage.py create_service_token ...`), then deploy the new pepper and switch the callers together. Rotate it only after a suspected leak |
| `OCTOREVIEW_ERASURE_HMAC_KEY` | Never rotate it: erased authors are recorded by its HMAC |
| S3 credentials | Use a role; botocore refreshes it. Rotate static keys by deploying the new pair |
