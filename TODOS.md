# TODOS

Staged in the `octoreview/` workspace during planning. The server repository's first commit moves
this file to `server/TODOS.md`. Each item was deferred by the 2026-11-18 /autoplan CEO review of
`octoreview-plan.md`.

## Product

### PRD-1: Helpfulness votes — DEFERRED

- **What:** "Was this review helpful?" votes per review, one per voter id, plus a
  `most_helpful` sort.
- **Why:** Long review lists need a quality signal beyond recency and stars.
- **Pros:** Better default ordering on busy entities.
- **Cons:** New table, new uniqueness rule, vote-stuffing defenses.
- **Context:** The v1 data model leaves room: votes hang off `Review` by id.
- **Effort:** human M / CC S. **Priority:** P2.
- **Trigger:** an integrating app asks for helpful-first sorting.

### PRD-2: Merchant / owner replies — DEFERRED

- **What:** One public reply per review from the entity owner, with its own moderation status.
- **Why:** Marketplaces expect sellers to answer reviews.
- **Pros:** Closes the loop for negative reviews.
- **Cons:** Needs an owner identity model (namespace + actor), new events and audit types.
- **Context:** The v1 namespace axis is the natural owner boundary.
- **Effort:** human M / CC S. **Priority:** P2.
- **Trigger:** an operator running a marketplace asks for seller replies. Gate rev 4 item q4
  (2026-11-18) keeps it deferred until then.

### PRD-3: Multi-criteria ratings — DEFERRED

- **What:** Optional per-criterion ratings (quality, delivery, value) configured per application.
- **Why:** Some domains rate several aspects.
- **Pros:** Richer summaries.
- **Cons:** Aggregates per criterion, config validation, SDK surface growth.
- **Context:** v1 keeps one overall 1..5 rating; criteria would be additive.
- **Effort:** human M / CC M. **Priority:** P3.
- **Trigger:** a product requirement names criteria.

### PRD-4: Weighted / Bayesian score — DEFERRED

- **What:** A `score` field that ranks entities fairly when review counts differ.
- **Why:** 5.0 from 1 review should not outrank 4.8 from 500.
- **Pros:** Sortable quality signal.
- **Cons:** Tunable prior; can be computed client-side from v1 summaries.
- **Effort:** human S / CC S. **Priority:** P3.
- **Trigger:** a listing page needs server-side ranking.

## Platform

### PLT-1: MCP server — DEFERRED

- **What:** An MCP server exposing review/rating reads and moderation actions to agents.
- **Why:** Agents could moderate and summarize reviews without custom glue.
- **Cons:** New auth surface; v1 agent-friendliness is covered by AGENTS.md + llms.txt.
- **Effort:** human L / CC M. **Priority:** P3.
- **Trigger:** an agent workflow needs live review data.

### PLT-2: Per-tenant storage backends — DEFERRED

- **What:** Let each tenant bring its own bucket/container.
- **Why:** Data-residency and cost isolation for large tenants.
- **Cons:** Credentials per tenant, rotation, key layout migration.
- **Context:** v1 stores `storage_backend` on every media row, so mixed backends stay readable.
- **Effort:** human L / CC M. **Priority:** P3.
- **Trigger:** a tenant requires its own storage account.

### PLT-3: Shared `octoverse-core` package — DEFERRED

- **What:** Extract the core, service_auth, audit and outbox code shared by octonomy and octoreview.
- **Why:** v1 copies that code; fixes must otherwise land twice.
- **Cons:** Touches a released service (octonomy) and couples release trains.
- **Effort:** human L / CC M. **Priority:** P3.
- **Trigger:** a third octoverse service, or the second security fix applied to both copies.

## Deployment

### DEP-1: systemd units — DEFERRED

- **What:** `deploy/systemd` units for web, outbox dispatcher and pending-media cleanup.
- **Why:** Bare-VM operators.
- **Cons:** Third deployment target to keep current.
- **Context:** octonomy ships `deploy/systemd`; copy and adapt when needed.
- **Effort:** human S / CC S. **Priority:** P3.
- **Trigger:** an operator deploys without containers.

### DEP-2: nginx `X-Accel-Redirect` for local-backend downloads — DEFERRED

- **What:** Let nginx serve local-backend media files after the server authorizes the signed
  token, via `OCTOREVIEW_LOCAL_ACCEL_REDIRECT_PREFIX` plus a shipped nginx config.
- **Why:** v1 streams local files through gunicorn (`FileResponse`), which ties up a worker thread
  per download.
- **Cons:** Needs an nginx deployment shape v1 does not ship; local backend is dev/single-node.
- **Effort:** human S / CC S. **Priority:** P3.
- **Trigger:** a single-node production deployment serves enough media to saturate workers.

## Added by the CEO dual-voice review (2026-11-18)

### PRD-5: Abuse reports on reviews — DEFERRED
- **What:** `POST /reviews/{id}/reports` (reason, reporter id) feeding the moderation queue.
- **Why:** Codex CEO voice: moderation needs an intake, not only status transitions.
- **Effort:** human M / CC S. **Priority:** P2. **Trigger:** first public-facing consumer.

### PRD-6: Entity merge and identifier migration — DEFERRED
- **What:** Merge two entities (aggregates, reviews, media keys) and rename `external_id`.
- **Why:** Catalog migrations change ids; reputation would otherwise split.
- **Effort:** human M / CC M. **Priority:** P3. **Trigger:** a consumer re-keys its catalog.

### PRD-7: `import_reviews` command — DEFERRED
- **What:** JSONL import keeping original timestamps, `--no-events`, dedupe on `source_ref`.
- **Why:** Switching cost is the main adoption barrier for review systems.
- **Effort:** human M / CC S. **Priority:** P2. **Trigger:** first adopter with existing reviews.

### PLT-4: Public read-only key + schema.org JSON-LD helper — DEFERRED
- **What:** A key type limited to published reads for storefront rendering, and an
  `AggregateRating`/`Review` JSON-LD helper in the JS SDK.
- **Why:** Storefront reads and SEO star snippets are the hot path.
- **Effort:** human M / CC S. **Priority:** P2. **Trigger:** first storefront integration.

### PLT-5: Resumable / multipart uploads — DEFERRED
- **What:** S3 multipart, Azure block blobs, GCS resumable sessions behind the same intent API.
- **Why:** v1 caps video at 500 MiB single PUT; large mobile uploads restart from zero.
- **Effort:** human L / CC M. **Priority:** P3. **Trigger:** a consumer needs videos above 500 MiB.

### PLT-6: Python SDK — DEFERRED
- **What:** `octoreview-python` mirroring the other SDKs.
- **Why:** A Python consumer (for example Octocart Core, Python/Django) would otherwise hand-roll
  HTTP calls.
- **Effort:** human M / CC S. **Priority:** P3. **Trigger:** a Python consumer commits to integrate.
- **Status (2026-11-18):** not triggered. Release line 1 ships the Go, PHP and JavaScript SDKs
  (owner correction), so the earlier "trigger fired" note and gate item q2 are withdrawn.

### PLT-7: PHP 7.4 compatibility line — CLOSED
- **What:** A rector-generated `support/php7.4` 1.x line, as octonomy-php built.
- **Why:** octonomy-php's one known prospect runs PHP 7.4.
- **Effort:** human M / CC S. **Priority:** P3. **Trigger:** a consumer on PHP below 8.2 commits
  to integrate.
- **Status (2026-11-19):** closed. The owner set the floor at PHP 8.2 (PHP 8.1 reached end of
  life on 2025-12-31), so octoreview-php ships one line requiring PHP 8.2 or later.

## Added by the Eng review (2026-11-18)

### DEP-3: Server-side rate limiting — DEFERRED
- **What:** Per-service-key DRF throttling with a shared cache backend, returning 429 + Retry-After.
- **Why:** One buggy key can flood intents or exhaust an application's quota.
- **Cons:** Needs a shared cache (Redis) in every deployment; v1 relies on a gateway.
- **Effort:** human S / CC S. **Priority:** P2. **Trigger:** first multi-team deployment.

## Added by the Eng rerun (2026-11-18)

### PLT-8: Storage migration command — DEFERRED
- **What:** Move media objects between storage locations and update `storage_location` ids.
- **Why:** One backend per deployment; changing provider later has no tool.
- **Effort:** human M / CC S. **Priority:** P3. **Trigger:** first provider change.

### PLT-9: Cursor pagination for review lists — DEFERRED
- **What:** Cursor (keyset) pagination; v1 caps offset at 10,000.
- **Why:** Deep offsets on popular entities are slow.
- **Effort:** human S / CC S. **Priority:** P3. **Trigger:** an entity with >10k reviews.

## Added by the Eng rerun, cycle 2 (2026-11-18)

### PRD-8: Video codec sniffing and playability warning — DEFERRED
- **What:** Read the MP4/MOV `stsd` box at `complete` and return `codec` plus a warning for HEVC
  (`hvc1`/`hev1`), which Chrome and Firefox often cannot play.
- **Why:** iPhones record HEVC by default; v1 accepts it (magic bytes pass) and only documents the
  playback risk (Eng cycle-2 N17).
- **Effort:** human S / CC S. **Priority:** P3. **Trigger:** an operator reports unplayable review
  videos, or asks for transcoding.

## Added by the Eng rerun, cycle 3 (2026-11-18)

### PLT-10: Write-once presigned uploads — DEFERRED
- **What:** Sign S3 `If-None-Match: *` and GCS `x-goog-if-generation-match: 0` into upload URLs so
  objects cannot change after the first PUT, and validate in place without the staging copy.
- **Why:** Removes the copy, lease and `upload_changed` path for providers that enforce it (Eng
  cycle-3 A2). Kept out of line 1 because Azure SAS cannot enforce it and one completion path for
  every backend is simpler to build and test.
- **Effort:** human M / CC S. **Priority:** P3. **Trigger:** copy cost or `complete` latency shows up
  in an operator's production metrics.

### PLT-11: Quota delta counter — DEFERRED
- **What:** An `ApplicationStorageUsage` row locked last and updated by deltas, plus a nightly
  `--check`, replacing the N2 `SUM` when a quota is set.
- **Why:** The SUM grows with an application's media rows (Eng cycle-3 E1); d19a was owner-confirmed.
- **Effort:** human S / CC S. **Priority:** P3. **Trigger:** the M2 measurement shows intent p95 over
  budget with a quota set.

### PLT-12: Audit log retention — DEFERRED
- **What:** `prune_audit_logs --older-than <days>` with a per-application default and an export hook.
- **Why:** `audit_logs` is append-only and grows without bound (Eng cycle-3 E7).
- **Effort:** human S / CC S. **Priority:** P3. **Trigger:** audit table above 10M rows or an
  operator's retention requirement.

### PLT-13: `reviews:read_unpublished` scope — DEFERRED
- **What:** A separate scope for reading unpublished review text (author path, detail of
  non-published reviews).
- **Why:** Today any `reviews:read` key can read pending or rejected text (Eng cycle-3 S5). All
  line-1 keys are server-side, so the risk is low.
- **Effort:** human S / CC S. **Priority:** P3. **Trigger:** a consumer issues read keys to a
  less-trusted surface.

## Added by the s3 backend (2026-11-19)

### PLT-14: Attempt-key fallback for stores without conditional copy — DEFERRED
- **What:** For a store that ignores `CopySourceIfMatch`, copy to a per-attempt final key
  (`{media_id}-{attempt_id}.{ext}`) persisted before the copy, and let `cleanup_media` delete the
  attempt keys no row committed (`docs/spec.md` 7.4, Eng N4).
- **Why:** Without it such a store would publish whatever the staging object holds at copy time.
- **Effort:** human M / CC S. **Priority:** P3. **Trigger:** a consumer's store fails
  `check_storage` at `conditional_copy`.
- **Status (2026-11-19):** not triggered. The week-1 spike found conditional copy on RustFS, MinIO
  and versitygw, and AWS S3 documents it (`docs/storage.md`).
