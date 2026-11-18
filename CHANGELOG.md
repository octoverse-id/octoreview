# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
