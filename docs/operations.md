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
| `media.rejected` | scope, `kind`, `backend`, `reason` | `complete` rejected the bytes: `size_mismatch`, `type_mismatch`, `over_limit` or `over_quota` |
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

## Alerts

| Alert | Condition | First look |
| --- | --- | --- |
| Outbox lag | `outbox.lag_seconds` `seconds` above 600 | Is the dispatcher running? Is the webhook receiver answering? |
| Storage errors | `storage.call` with `outcome` `error` above 5% of all `storage.call` for 5 minutes | `manage.py check_storage` names the failing step; see below |
| Cleanup failing | `cleanup_media.failed` `count` above 0 on every run for 1 hour | The command lists the failed keys on stderr and exits 1 |
| Dead letters | `outbox.dead_letter_count` `count` above 0 | Fix the receiver, then `dispatch_outbox_events --retry-failed` |

## Scheduled jobs

Run each as a Kubernetes CronJob with `concurrencyPolicy: Forbid` (or cron on a single host),
with the same image and environment as the server:

| Command | Schedule | Why |
| --- | --- | --- |
| `python manage.py dispatch_outbox_events` | every minute | delivers events (webhook transport); `--retry-failed` also retries failed ones |
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

## Rotating secrets

| Secret | How |
| --- | --- |
| `OCTOREVIEW_MEDIA_TOKEN_SECRET` (local backend) | Move the current value to `OCTOREVIEW_MEDIA_TOKEN_SECRET_PREVIOUS`, set a new one, deploy; drop the previous value after the longest URL TTL has passed |
| Service keys | `create_service_token` a new key, switch the caller, then `revoke_service_token` the old prefix |
| `DJANGO_SECRET_KEY` | Replace and deploy; it signs nothing octoreview's clients keep |
| `OCTOREVIEW_SERVICE_TOKEN_PEPPER` | Rotating it invalidates every service key; issue new keys first |
| `OCTOREVIEW_ERASURE_HMAC_KEY` | Never rotate it: erased authors are recorded by its HMAC |
| S3 credentials | Use a role; botocore refreshes it. Rotate static keys by deploying the new pair |
