# Octoreview Agent Instructions

Octoreview is a standalone, multi-tenant, multi-application review and rating service. Every
entity can be reviewed and rated; reviews can carry images and videos whose per-entity limits are
configurable, and media bytes live in a configurable storage backend.

## Status

This repository was bootstrapped with governance files and a CI skeleton. The approved plan
(octoreview workspace `octoreview-plan.md`, reviewed with /autoplan on 2026-11-18) drives the
work. Release line 1 targets the first consumer, Mbizmarket (Go, PHP and JavaScript stack, media
on AWS S3 or an S3-compatible store).

The first feature PR writes `docs/spec.md`, the spec of record. Once it exists, AGENTS.md and
every doc link to it instead of restating it, and it wins over any other description.

## Product Rules

- Octoreview stores reviews, ratings, review media and per-entity review configuration. It does
  not store or duplicate external entity data (names, prices, descriptions stay with the caller).
- An **entity** is an external resource identified by `(entity_type, external_id)` inside a
  tenant, application and optional namespace, for example `product/sku-123` or `merchant/m-9`.
- All tenant-owned data is scoped by `tenant_id`. `application_id` is required on entities,
  reviews and media.
- Every entity can be reviewed and rated. A review carries one integer rating from 1 to 5 and an
  optional title and body. One active review per `(entity, author_id)`, or per
  `(entity, author_id, source_ref)` when the caller sets `source_ref` (an order or booking id).
- A review can carry images and videos. The maximum number of images and videos per review is
  configured per entity, falling back to the application default, then the server default.
- Media bytes live in a configurable storage backend: local filesystem, AWS S3 (and any
  S3-compatible store such as MinIO, Cloudflare R2, DigitalOcean Spaces, Wasabi), Azure Blob
  Storage, or Google Cloud Storage. The database stores only the storage key and metadata.
- Rating aggregates (count, average, 1-5 histogram) count **published** reviews only and are
  updated in the same transaction as the review change.
- Review deletion is a status change (`removed`), never a hard delete. Media deletion removes the
  object from storage and keeps the row as `deleted` for audit.

## API Rules

- REST (`/api/v1`) is the only API surface in v1. No GraphQL.
- Maintain OpenAPI coverage for every public endpoint (`docs/openapi.yaml`).
- Errors use the octonomy envelope (`error.code`, `error.message`, `error.details`,
  `error.request_id`) plus `doc_url` and `retryable`.
- Lists use limit/offset with the `{data, pagination}` envelope.

## Backend Conventions

- Django 5.2 LTS, Django REST Framework, PostgreSQL in production (SQLite also in CI), UUID
  primary keys, uv + ruff + pytest.
- Business rules live in services, not views or serializers; tenant isolation is explicit in
  every query.
- Explain non-obvious invariants with a comment next to the code that enforces them.
- Generic pieces (request ids, auth, errors, pagination, service_auth, audit, outbox) are copied
  from octonomy, never imported at runtime. Record the octonomy commit each copied module came
  from in `docs/architecture.md`.
- Port security fixes to the sibling service: a security fix in a module copied from octonomy is
  checked against octonomy (and the other way round) in the same PR's checklist.

## Gates

- `make help` lists every target. `make check` is the fast gate and needs only the language
  toolchain (no Docker, no network). `make release-check` is the full gate (needs Docker once
  integration targets land) and fails while any CI job is still a bootstrap placeholder.
- CI runs the same targets. Required check names: `lint`, `checks`, `security`, `test-sqlite`, `test-postgres`, `storage-emulators`, `compose-quickstart`, `docker`.

## Development Pipeline

- Branch names follow Conventional Branch naming (https://conventional-branch.github.io/):
  `<type>/<description>`, lowercase alphanumerics, hyphens and dots. Allowed types: `feature`,
  `feat`, `bugfix`, `fix`, `hotfix`, `release`, `chore`.
- Planned work starts with a GitHub issue. When the user asks to implement an approved plan,
  create the issue before the branch (or use the issue number the user gives); it carries the plan
  summary, key tasks and acceptance checks. If issue creation fails, stop and report the blocker.
- Planned-work branches include the issue number: `<type>/<issue-number>-<short-description>`,
  for example `feature/12-review-media`.
- PR bodies include `Closes #<issue-number>` and map the change back to the plan.
- `main` is protected by the `main: require PR + green CI` ruleset: every change lands through a
  PR whose required checks are green; nobody bypasses it.
- A PR that adds or renames a required CI job follows this order: wait for the PR's CI to report
  the new check name; update the live ruleset (a human with repo admin; agents stop and ask);
  run `make export-rulesets` and commit the mirror diff to the same PR; merge; tell other open PRs
  to rebase.
- Releases follow Semantic Versioning with the policy in `docs/versioning.md` and the runbook in
  `docs/release.md`. Version bumps and tags happen only in a dedicated `release/<version>` PR,
  never in feature or fix PRs. Release tags never move.
- The `code-review/` directory is reserved for local review pipeline artifacts. Review agents
  write findings to `code-review/findings.md`; patch agents read it, apply valid fixes and write
  `code-review/patches.md`. Never stage or commit anything under `code-review/` except the tracked
  `code-review/.gitkeep`; after creating a PR, delete the local artifacts.

## Web Browsing

- Use the `/browse` skill from gstack for all web browsing.
- Do not use `mcp__claude-in-chrome__*` tools.
