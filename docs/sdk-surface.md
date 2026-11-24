# SDK surface

Generated from `docs/sdk-surface.yaml` by `make sdk-surface`; edit the YAML, not this
file. `make openapi-check` fails when it is stale.

## Retry classes

| Class | Rule |
| --- | --- |
| `safe` | A read. The SDK retries on timeouts, 503 and 429, honoring Retry-After. |
| `idempotent` | Repeating a success leaves the same state and answer. The SDK retries like a read; completeMediaUpload also retries 409 complete_in_progress and upload_changed, as its OpenAPI description says. |
| `keyed` | Idempotent when the idempotency field is sent. The SDK generates one when the caller does not, then retries like a read. |
| `unsafe` | Never retried automatically. After a lost response a repeat may answer 409; read the resource to learn the outcome. |

## Operations

| Operation | Go | PHP | JS | Retry | Required | Optional |
| --- | --- | --- | --- | --- | --- | --- |
| `getHealthLive` | `Health.Live` | `health()->live` | `health.live` | safe |  |  |
| `getHealthReady` | `Health.Ready` | `health()->ready` | `health.ready` | safe |  |  |
| `listEntities` | `Entities.List` | `entities()->list` | `entities.list` | safe |  | `application_id`, `include_global`, `entity_type`, `external_id`, `is_active`, `limit`, `offset` |
| `upsertEntity` | `Entities.Upsert` | `entities()->upsert` | `entities.upsert` | idempotent | `application_id`, `entity_type`, `external_id` | `metadata`, `max_images`, `max_videos`, `max_image_bytes`, `max_video_bytes` |
| `lookupEntity` | `Entities.Lookup` | `entities()->lookup` | `entities.lookup` | safe | `application_id`, `entity_type`, `external_id` | `include_global` |
| `getEntity` | `Entities.Get` | `entities()->get` | `entities.get` | safe | `entity_id` | `application_id`, `include_global` |
| `updateEntity` | `Entities.Update` | `entities()->update` | `entities.update` | idempotent | `entity_id` | `application_id`, `metadata`, `max_images`, `max_videos`, `max_image_bytes`, `max_video_bytes`, `is_active` |
| `deactivateEntity` | `Entities.Deactivate` | `entities()->deactivate` | `entities.deactivate` | idempotent | `entity_id` | `application_id` |
| `getEntityRating` | `Ratings.ForEntity` | `ratings()->forEntity` | `ratings.forEntity` | safe | `entity_id` | `application_id`, `include_global` |
| `queryRatings` | `Ratings.Query` | `ratings()->query` | `ratings.query` | safe | `application_id`, `entity_type`, `external_ids` | `include_global` |
| `getApplicationSettings` | `Settings.Get` | `settings()->get` | `settings.get` | safe | `application_id` |  |
| `putApplicationSettings` | `Settings.Put` | `settings()->put` | `settings.put` | idempotent | `application_id`, `max_images`, `max_videos`, `max_image_bytes`, `max_video_bytes`, `allowed_content_types`, `moderation_mode`, `max_storage_bytes` |  |
| `listReviews` | `Reviews.List` | `reviews()->list` | `reviews.list` | safe |  | `application_id`, `include_global`, `entity_id`, `entity_type`, `external_id`, `author_id`, `include_unpublished`, `status`, `rating`, `has_media`, `is_verified_purchase`, `has_pending_revision`, `media_moderation_status`, `sort`, `limit`, `offset` |
| `createReview` | `Reviews.Create` | `reviews()->create` | `reviews.create` | unsafe | `application_id`, `author_id`, `rating` | `entity_id`, `entity_type`, `external_id`, `title`, `body`, `metadata`, `author_metadata`, `source_ref`, `is_verified_purchase` |
| `getReview` | `Reviews.Get` | `reviews()->get` | `reviews.get` | safe | `review_id` | `application_id`, `include_global`, `author_id`, `include_unpublished` |
| `updateReview` | `Reviews.Update` | `reviews()->update` | `reviews.update` | unsafe | `review_id` | `application_id`, `rating`, `title`, `body`, `metadata`, `author_metadata`, `is_verified_purchase` |
| `removeReview` | `Reviews.Remove` | `reviews()->remove` | `reviews.remove` | unsafe | `review_id` | `application_id` |
| `publishReview` | `Reviews.Publish` | `reviews()->publish` | `reviews.publish` | unsafe | `review_id`, `expected_version`, `expected_media_version` | `application_id`, `reason` |
| `rejectReview` | `Reviews.Reject` | `reviews()->reject` | `reviews.reject` | unsafe | `review_id` | `application_id`, `reason` |
| `hideReview` | `Reviews.Hide` | `reviews()->hide` | `reviews.hide` | unsafe | `review_id` | `application_id`, `reason` |
| `getReviewRevision` | `Reviews.GetRevision` | `reviews()->getRevision` | `reviews.getRevision` | safe | `review_id` | `application_id` |
| `approveReviewRevision` | `Reviews.ApproveRevision` | `reviews()->approveRevision` | `reviews.approveRevision` | unsafe | `review_id`, `revision_id`, `expected_version`, `expected_media_version` | `application_id` |
| `rejectReviewRevision` | `Reviews.RejectRevision` | `reviews()->rejectRevision` | `reviews.rejectRevision` | unsafe | `review_id`, `revision_id` | `application_id` |
| `listReviewMedia` | `Media.List` | `media()->list` | `media.list` | safe | `review_id` | `application_id`, `include_global`, `author_id`, `include_unpublished`, `limit`, `offset` |
| `createMediaUpload` | `Media.CreateUpload` | `media()->createUpload` | `media.createUpload` | keyed (`client_upload_id`) | `review_id`, `kind`, `content_type`, `size_bytes` | `application_id`, `client_upload_id`, `position`, `width`, `height`, `duration_seconds` |
| `completeMediaUpload` | `Media.Complete` | `media()->complete` | `media.complete` | idempotent | `review_id`, `media_id` | `application_id` |
| `deleteMedia` | `Media.Delete` | `media()->delete` | `media.delete` | idempotent | `review_id`, `media_id` | `application_id` |
| `approveMedia` | `Media.Approve` | `media()->approve` | `media.approve` | unsafe | `review_id`, `media_id`, `expected_version`, `expected_media_version` | `application_id`, `reason` |
| `rejectMedia` | `Media.Reject` | `media()->reject` | `media.reject` | idempotent | `review_id`, `media_id` | `application_id`, `reason` |
| `uploadMediaBytes` | — | — | — | — | The upload helpers PUT to `upload.url` as returned; it is never built from this path. | |
| `downloadMedia` | — | — | — | — | Media `url` values are handed to browsers and players as returned. | |
| `eraseAuthor` | `Authors.Erase` | `authors()->erase` | `authors.erase` | idempotent | `application_id`, `author_id` |  |
| `listAuditLogs` | `AuditLogs.List` | `auditLogs()->list` | `auditLogs.list` | safe |  | `application_id`, `include_global`, `action`, `aggregate_type`, `aggregate_id`, `actor_id`, `limit`, `offset` |

## Helpers

| Helper | Go | PHP | JS | Calls | What it does |
| --- | --- | --- | --- | --- | --- |
| `uploadFile` | `Media.UploadFile` | `media()->uploadFile` | `media.uploadFile` | `createMediaUpload`, `completeMediaUpload`, `deleteMedia` | Intent, PUT and complete for a file path; infers content type and size. |
| `uploadBytes` | `Media.UploadBytes` | `media()->uploadBytes` | `media.uploadBytes` | `createMediaUpload`, `completeMediaUpload`, `deleteMedia` | Intent, PUT and complete for an in-memory body and a content type. |
| `uploadStream` | `Media.UploadStream` | `media()->uploadStream` | `media.uploadStream` | `createMediaUpload`, `completeMediaUpload`, `deleteMedia` | Intent, PUT and complete for a rewindable stream with an explicit size. |
| `ratingsByKey` | `Ratings.Get` | `ratings()->get` | `ratings.get` | `lookupEntity` | The rating summary of `{entityType, externalId}` without knowing the entity id. |
| `verifyWebhook` | `Webhooks.Verify` | `webhooks()->verify` | `webhooks.verify` | none (local) | Check `X-Octoreview-Signature` (300 s tolerance) and decode the typed event. |
