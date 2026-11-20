# Architecture

[`docs/spec.md`](spec.md) says what the service does; this page says where each piece lives.

## Layer map

Layers run bottom to top; a module imports only from layers below it. `make checks` enforces
this with import-linter (`[tool.importlinter]` in `pyproject.toml`).

| Layer | Module | Owns |
| --- | --- | --- |
| top | `octoreview.openapi` | `make openapi-check`: compares `docs/openapi.yaml`, `errors.md`, `events.md` and `sdk-surface.yaml` with the URLconf and the error and event registries |
| | `octoreview.api`, `octoreview.console` | Presentation, independent of each other. `api`: views, serializers and URL routes. `console`: the opt-in admin's registrations, moderation actions through the lifecycle services, and its system check |
| | `octoreview.lifecycle` | Transition tables, removal cascades, erasure (release line 1) |
| | `octoreview.media` | Media rows, upload intents, completion, signed tokens; `media.storage` holds the adapters (`local`, `s3`) and nothing else imports it; boto3 is imported only by `media.storage.s3` |
| | `octoreview.reviews` | Reviews and revisions (release line 1) |
| | `octoreview.entities` | Entities, application settings, rating aggregates (release line 1) |
| | `octoreview.service_auth` | Service clients, grants, key hashing, the DRF permission class, token commands |
| | `octoreview.audit`, `octoreview.events` | Audit log; transactional outbox, dispatcher and transports |
| bottom | `octoreview.core` | Request ids, error envelope and code registry, pagination, namespace parsing, grant predicates, JSON logging, metrics |

`config/` holds settings (`settings.py`, `settings_pytest.py`), the root URLconf (which mounts
`/admin/` only when `OCTOREVIEW_ADMIN_ENABLED`), and the admin site wiring (`admin.py`,
`adminsite.py`, `auth_admin.py`).

## Request path

```text
RequestContextMiddleware   request id (100-char rule), tenant header, route-template request log
        │
DRF view (octoreview.api)  @require_scopes declares the scope per method
        │
BearerTokenPermission      key → ServiceClient; X-Tenant-ID; namespace headers; canonical
(service_auth)             application_id; grant predicate (core.auth.grant_authorizes)
        │
domain services            secondary scopes via core.auth.require_permission
        │
audit row + outbox event   written in the same transaction as the change
        │
error envelope             core.errors.exception_handler for every failure, 500s included
```

## Copied from octonomy

These modules are copied from octonomy at commit `82bcd0da6e44c3bf10784028d5bea01c74a51ca0`
(2026-10-13) and adapted; octoreview never imports octonomy at runtime. When a security fix
lands in one of these modules, check the sibling service in the same PR (AGENTS.md).

| octoreview module | octonomy source | Changes |
| --- | --- | --- |
| `core/models.py` | `core/models.py` | adds `SCOPE_ID_PATTERN` (ids start with an alphanumeric) |
| `core/errors.py` | `core/errors.py` | registry of every code; `doc_url` and `retryable`; 401 split; 500 envelope |
| `core/pagination.py` | `core/pagination.py` | default 20, max 100, offset cap 10000, no next/previous links |
| `core/middleware.py` | `core/middleware.py` | inbound request ids validated to 100 characters; route template instead of path |
| `core/logging.py` | `core/logging.py` | scrubs token paths, signed query parameters, bearer and service keys |
| `core/metrics.py` | `core/metrics.py` | metric names for octoreview |
| `core/auth.py`, `core/namespaces.py` | `core/auth.py`, `core/versioning.py` | no v2 surface or rollout switches; `application_mismatch` and application-wide grants per spec section 2 |
| `service_auth/models.py` | `service_auth/models.py` | unchanged |
| `service_auth/services.py` | `service_auth/services.py` | `ocr` key prefix; unknown scopes refused |
| `service_auth/permissions.py` | `core/auth.py` (`BearerTokenPermission`) | moved above `core`; 403 details `reason`, `required_scope`, `grant_scope` |
| `service_auth/management/commands/*` | same | `--json`; unknown scope exits 2 listing valid scopes |
| `audit/models.py`, `audit/services.py` | same | `aggregate_type`/`aggregate_id`; no tag columns; a missing audit context is a bug, not a skipped row |
| `events/models.py`, `events/services.py` | same | `resource_type`/`resource_id`; no actor column; payloads refuse personal fields |
| `events/dispatch.py` | `events/dispatch.py` | envelope-only logging; timestamped webhook signature; scheduled runs retry failed events after their backoff, and `--retry-failed` is an operator replay that also takes dead letters; delivered `author.erased` deleted; no namespace kill-switch |
| `Dockerfile` | `Dockerfile` | Python 3.12 (octonomy: 3.14); `uv sync --extra s3`; a writable `/var/lib/octoreview/media` for uid 1001; no `collectstatic` (the server renders JSON only); a `.dockerignore` |
| `docker-entrypoint.sh` | `docker-entrypoint.sh` | comments only |
| `gunicorn.conf.py` | `gunicorn.conf.py` | `gthread` with 8 threads by default (octonomy: `sync`, 1; spec section 7.4); formatter from `octoreview.core.logging` |
| `.github/workflows/publish-image.yml`, `.github/actions/build-publish/action.yml` | same | `vX.Y.Z-rc.N` tags publish (only their own tag); the version is read from PEP 440; `docker-smoke` on amd64 and octoreview's settings on arm64; `:edge` also gets `:sha-<commit>`; version stamps and the release-merge check skip dry runs; a dry run tolerates no release tags yet |
| `scripts/check-release-merge.sh`, `scripts/check-tag-unpublished.sh`, `scripts/resolve-latest-tag.sh` and `tests/tooling/` | same | candidates accepted; PEP 440 `rcN` read as semver `-rc.N`; a candidate resolves to its own tag only; rc tests replace octonomy's "no prerelease rules" test |
| `config/admin.py`, `config/adminsite.py`, `config/auth_admin.py` | same | renamed; the site drops octonomy's Swagger link (octoreview has no docs UI) |
| `console/base.py` | `core/admin_base.py` | `ReadOnlyModelAdmin` and `pretty_json` kept; a `ModerationModelAdmin` that runs one lifecycle service per row replaces the service-backed form admin |
| `console/admin.py` (service clients, grants, audit, outbox) | `service_auth/admin.py`, `audit/admin.py`, `events/admin.py` | octoreview's audit and outbox columns; the same safe-field allowlist for service clients |
| `console/checks.py` (`octoreview.W001`) | `core/checks.py` (`octonomy.W001`) | wording |
| `tests/console/conftest.py` | `tests/admin/conftest.py` | `console` and `root_client` fixtures |
| `ci.yml` job `docker` | `ci.yml` job `docker` | the smoke steps live in `scripts/docker-smoke.sh` (`make docker-smoke`) and add non-root, no-dev-tools, s3-extra, fail-closed, ready-after-migrate, gthread and all-JSON-log checks |
