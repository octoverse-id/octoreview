# Architecture

[`docs/spec.md`](spec.md) says what the service does; this page says where each piece lives.

## Layer map

Layers run bottom to top; a module imports only from layers below it. `make checks` enforces
this with import-linter (`[tool.importlinter]` in `pyproject.toml`).

| Layer | Module | Owns |
| --- | --- | --- |
| top | `octoreview.api` | Views, serializers and URL routes; composes the domain modules |
| | `octoreview.lifecycle` | Transition tables, removal cascades, erasure (release line 1) |
| | `octoreview.media` | Media rows, upload intents, completion, signed tokens; `media.storage` holds the adapters and nothing else imports it |
| | `octoreview.reviews` | Reviews and revisions (release line 1) |
| | `octoreview.entities` | Entities, application settings, rating aggregates (release line 1) |
| | `octoreview.service_auth` | Service clients, grants, key hashing, the DRF permission class, token commands |
| | `octoreview.audit`, `octoreview.events` | Audit log; transactional outbox, dispatcher and transports |
| bottom | `octoreview.core` | Request ids, error envelope and code registry, pagination, namespace parsing, grant predicates, JSON logging, metrics |

`config/` holds settings (`settings.py`, `settings_pytest.py`) and the root URLconf.

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
| `events/dispatch.py` | `events/dispatch.py` | envelope-only logging; timestamped webhook signature; dead-letter replay; delivered `author.erased` deleted; no namespace kill-switch |
