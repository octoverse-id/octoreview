# Octoreview

[![CI](https://github.com/octoverse-id/octoreview/actions/workflows/ci.yml/badge.svg)](https://github.com/octoverse-id/octoreview/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Release](https://img.shields.io/github/v/tag/octoverse-id/octoreview?sort=semver&filter=!*-rc*&label=release)](https://github.com/octoverse-id/octoreview/tags)

Octoreview adds ratings and photo or video reviews to anything in your product: products,
merchants, listings, courses. Your backend sends the reviews; Octoreview keeps them, the 1-5
ratings and their aggregates, and the review media in your own storage.

It is a standalone REST service. It is multi-tenant and multi-application, and it scopes every
entity by tenant, application and optional namespace. An entity is identified by your own
`(entity_type, external_id)`, for example `product/sku-123`. Octoreview never stores the entity's
own data: names, prices and descriptions stay in your system.

**When to use it:**

- You want the review data in your own database and the media in your own storage.
- You serve several tenants or apps from one service.
- You need rating aggregates updated with each review, webhooks for review events, author
  erasure, or the Go, PHP and JavaScript SDKs.

**When not to:**

- A single app with text-only reviews: a table in your own database is enough.
- Hosted review widgets, syndication or SEO rich snippets: a hosted review service fits better.
  Octoreview has no JSON-LD output yet (TODOS PLT-4).

> **Project status:** Stable, release line 1 ([CHANGELOG](CHANGELOG.md)), following
> [Semantic Versioning](docs/versioning.md). `/api/v1` gets no breaking changes in line 1.
> Storage backends are `local` and `s3`. AWS S3 is beta until an operator's run on a real bucket
> passes; the other S3-compatible stores are listed in
> [`docs/storage.md`](docs/storage.md#compatible-stores). Azure Blob Storage and Google Cloud
> Storage are planned for release line 2. If you run Octoreview on your own bucket, run the
> smoke test there and report it ([Operator smoke runs](docs/release.md#operator-smoke-runs)):
> that run takes AWS S3 out of beta.

## Features

- **One review per author.** One 1-5 rating with an optional title and body per author, or per
  author and `source_ref` (an order or booking id) ([spec 3.3](docs/spec.md#33-review)).
- **Rating aggregates.** Count, average and a 1-5 histogram over published reviews, updated in
  the same transaction as the review; summaries for up to 100 entities in one call
  ([spec 3.2](docs/spec.md#32-entity), [4.1](docs/spec.md#41-review)).
- **Photo and video reviews.** Upload straight to storage through a signed URL, with image and
  video limits per entity, falling back to the application, then the server
  ([spec 6](docs/spec.md#6-media-policy), [7](docs/spec.md#7-storage-and-the-upload-protocol)).
- **Moderation.** Auto-publish or pre-moderation per application, with moderated edits
  ([spec 3.1](docs/spec.md#31-applicationsettings), [4.2](docs/spec.md#42-edits-and-revisions)).
- **Events.** A transactional outbox delivered to your webhook, signed
  ([spec 10](docs/spec.md#10-events-and-webhooks)).
- **Author erasure.** Erase everything one author wrote, media included
  ([spec 9](docs/spec.md#9-erasure-post-authorserase-managepy-erase_author)).
- **Operator console.** An optional admin console for lookups and moderation
  ([`docs/operations.md`](docs/operations.md#admin-console)).
- **HTTP caching.** `ETag` and `304 Not Modified` on reads ([spec 5.3](docs/spec.md#53-caching)).

## Quickstart

This path uses Docker. It took about 3 minutes from `git clone` to the first review with a photo
(132 seconds, most of it the image build), measured on an x86_64 laptop (Intel Core i7-1165G7,
WSL2 with Docker Desktop) with the Python and PostgreSQL base images already pulled. The first
pull of those images adds its download time.

Prerequisites: Docker with Compose v2 (tested with Compose 5.6), `make` and `git`; `bash`, `curl`
and `python3` on the host for `make quickstart`.

```sh
git clone https://github.com/octoverse-id/octoreview.git
cd octoreview
make up          # builds the image, starts octoreview + PostgreSQL, writes .octoreview-dev.env
make quickstart  # creates a review, uploads a photo, reads the rating
```

`make quickstart` prints:

```text
review: 3853cced-30a8-4233-a86e-94bf5de70e4c
rating: average 5.0 from 1 published review(s)
photo: http://127.0.0.1:8000/api/v1/media-files/...
```

`.octoreview-dev.env` holds the base URL, a demo key with every scope, the tenant (`demo`) and
the application (`storefront`). It is git-ignored; the SDK quickstarts read it too.

To skip the build, start the published image instead of `make up` (12 seconds here, once the
image is pulled):

```sh
OCTOREVIEW_IMAGE=ghcr.io/octoverse-id/octoreview:1.0.0 docker compose up -d --wait --no-build
make dev-env     # copies .octoreview-dev.env out of the stack
make quickstart
```

Set `OCTOREVIEW_IMAGE` on that one command and never export it: an exported value makes a later
`make up` tag its local build with the published name.

Your first call of your own, with the values `make up` wrote:

```sh
. ./.octoreview-dev.env
curl -sS "$OCTOREVIEW_BASE_URL/api/v1/reviews?application_id=$OCTOREVIEW_APPLICATION_ID&entity_type=product&external_id=sku-quickstart&limit=1" \
  -H "Authorization: Bearer $OCTOREVIEW_API_KEY" \
  -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID"
```

[API examples](#api-examples) has more.

### What is running

| Service | Address | Notes |
| --- | --- | --- |
| API | http://127.0.0.1:8000/api/v1/ | `OCTOREVIEW_PORT` changes the port |
| Health | http://127.0.0.1:8000/api/v1/health/live, `/health/ready` | no key needed |
| Admin console | http://127.0.0.1:8000/admin/ | needs a login, below |
| PostgreSQL | inside the compose network only | not published |
| RustFS (S3 emulator) | http://127.0.0.1:9000 | only with `make up-s3`; `OCTOREVIEW_S3_PORT` changes the port |

The ports listen on 127.0.0.1 only, because the stack runs with DEBUG on and the emulator uses
fixed credentials. `OCTOREVIEW_BIND=0.0.0.0 make up` publishes the ports on your LAN, but the API
and photo URLs still name 127.0.0.1, so another device reaches the API and not the photos.

Create an admin console login, interactively:

```sh
docker compose exec octoreview python manage.py createsuperuser
```

or without prompts (type the password, then Enter):

```sh
read -rs DJANGO_SUPERUSER_PASSWORD; export DJANGO_SUPERUSER_PASSWORD
docker compose exec -T -e DJANGO_SUPERUSER_PASSWORD octoreview python manage.py createsuperuser --noinput --username admin --email admin@example.com
unset DJANGO_SUPERUSER_PASSWORD
```

### Media on S3, ports and stopping

- `make up-s3` runs the same stack with media on the RustFS S3 emulator, the way a cloud
  deployment stores them. Switch backends on a clean stack, because stored media keep their
  storage location: `make down`, then `docker compose down -v`, then `make up-s3`. Switch back
  the same way with `make up`.
- When port 8000 or 9000 is taken, export `OCTOREVIEW_PORT` or `OCTOREVIEW_S3_PORT` (for example
  `export OCTOREVIEW_PORT=18000`) and run `make up` again; it rewrites `.octoreview-dev.env`.
- `make down` stops the stack and keeps its data; `docker compose down -v` also drops the data.

The stack is for development: DEBUG is on and the demo key has every scope. To run Octoreview
for real, see [Installation (production)](#installation-production). When a step fails, see
[Troubleshooting](#troubleshooting).

## Local development without Docker

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.

```sh
make install     # the project with its dev and s3 extras
make run         # http://127.0.0.1:8000 on SQLite, media in ./var/media
```

`make run` prints `make run: database sqlite (db.sqlite3), http://127.0.0.1:8000, storage local`,
migrates, writes `.octoreview-dev.env` with a demo key and serves on 127.0.0.1 only. Then, in a
second shell:

```sh
make quickstart
```

`make run` ignores `PORT` and `DATABASE_URL` from your shell; change them on the make command
line: `make run PORT=8001`, or `make run DATABASE_URL=postgres://...` for PostgreSQL. The server
reads only the process environment and loads no `.env` file. Do not export `.env.example` into
your shell: its `DATABASE_URL` points at a local PostgreSQL, which other targets would then use.

Before a PR, run `make check` (lint, Django checks, the version gate, the docs link check and the
tests; no Docker or network).

To start over, stop `make run`, then:

```sh
rm -rf db.sqlite3 var/media .octoreview-dev.env
```

### Troubleshooting

- **`Bind for 127.0.0.1:8000 failed: port is already allocated`** (`make up`): another program
  holds the port. Export `OCTOREVIEW_PORT` (or `OCTOREVIEW_S3_PORT` for 9000), then run
  `make up` again.
- **`make run: port 8000 is taken; run make run PORT=<other port>`**: run `make run PORT=8001`.
  Its seed keeps the key and rewrites the base URL in `.octoreview-dev.env`.
- **`quickstart: .octoreview-dev.env is missing; with Docker run make up, without Docker start
  make run (both write it)`**: run `make up`, or start `make run` in another shell.
- **`quickstart: FAILED at create_review (HTTP 401, ...)` followed by `quickstart: the key in
  .octoreview-dev.env is not live on ...`**: each path has its own database, so a key from one
  is unknown to the other. With Docker, run `make dev-env`; without Docker, restart `make run`,
  whose seed replaces a key that is not live.
- **`make dev-env: the stack is not running; start it with make up`**: run `make up`.
- **`make up` fails while it waits for the stack**: `docker compose logs octoreview` names the
  cause.
- **`seed refuses a real AWS bucket; use the local backend or the compose S3 emulator.`**: the
  demo key is for development, so the seed runs only on the `local` backend (`make run`,
  `make up`) or the S3 emulator (`make up-s3`).
- **`OCTOREVIEW_STORAGE_LOCATION_ID is required; ...`** when you run `manage.py` by hand: use
  `make run`, or export the values it sets (listed in the `Makefile`).
- **Asking for help:** share the request id of the failed call (`quickstart` prints it; every
  response carries `X-Request-ID`), never `curl -v` output, which prints the key.

## Installation (production)

The image is published to the GitHub Container Registry:

```sh
docker pull ghcr.io/octoverse-id/octoreview:1.0.0
```

| Tag | Use |
| --- | --- |
| `X.Y.Z` | pin this in production |
| `X.Y` | the newest patch release of a line |
| `latest` | the newest release |
| `edge` | `main`'s tip; not for production |

[`docs/versioning.md`](docs/versioning.md) lists every tag and when it moves.

### Single host with Docker Compose

Use the `deploy/` files of the release tag that matches the image, because `deploy/` on `main`
can carry a release candidate's tag:

```sh
git clone --depth 1 --branch v1.0.0 https://github.com/octoverse-id/octoreview.git
cd octoreview
cp deploy/.env.production.example deploy/docker/.env && chmod 600 deploy/docker/.env
```

Fill in `deploy/docker/.env`; each line is commented, and
[`.env.example`](.env.example) explains every setting:

- **Project name.** On a first install, add `COMPOSE_PROJECT_NAME=octoreview-prod`. Without it
  the project takes the directory name, `docker`, and replaces the containers of any other
  project with that name. The development stack is the project `octoreview`; both own `db`,
  `pgdata` and `media`. An existing install keeps its current name (unset means `docker`, with
  the volume `docker_pgdata`), because a new name starts an empty database.
- **Secrets.** `DJANGO_SECRET_KEY`, `OCTOREVIEW_SERVICE_TOKEN_PEPPER` and
  `OCTOREVIEW_ERASURE_HMAC_KEY`, each from
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **Database.** `POSTGRES_PASSWORD`, and the same password in `DATABASE_URL`. PostgreSQL keeps
  the password it was first created with in the `pgdata` volume.
- **Hosts.** `DJANGO_ALLOWED_HOSTS`: your hostname, keeping `127.0.0.1` for the health checks.
- **Storage.** The file defaults to `s3` (bucket, region, credentials; see
  [`docs/storage.md`](docs/storage.md)). For media on this host, set
  `OCTOREVIEW_STORAGE_BACKEND=local` and uncomment its three lines.
- **Events.** `OCTOREVIEW_OUTBOX_TRANSPORT=webhook` with your receiver's URL and secret. Without a
  receiver yet, `logging` writes each event to the log and marks it delivered. Switching to the
  webhook later does not replay those events, so if you need every event (notably
  `author.erased`, whose outbox row is deleted after delivery; see
  [`docs/events.md`](docs/events.md)), configure the receiver before you accept writes.
- **Admin console.** The file keeps it off (`OCTOREVIEW_ADMIN_ENABLED=false`); the console login
  in the quickstart is for the development stack.

Start it. On a host that also runs the development stack, stop that first with `make down`,
because both publish 127.0.0.1:8000.

```sh
cd deploy/docker
docker compose up -d --wait
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
```

`--wait` returns once the one-shot migration exited 0 and the API is healthy. When it fails,
`docker compose logs migrate app` names the cause. On a first install, a wrong database password
stays in the volume: drop it with `docker compose -p octoreview-prod down -v` (this deletes the
new install's data), fix `.env` and start again.

Then put a TLS reverse proxy in front of 127.0.0.1:8000
([`docs/deployment.md`](docs/deployment.md#reverse-proxy-and-load-balancer)) and create a service
key ([Authentication](#authentication-and-tenant-scope)). Run the
[smoke test](docs/operations.md#smoke-test) after each deployment; it needs a key for a
dedicated `smoke` application, not your storefront key.

[`docs/deployment.md`](docs/deployment.md#single-host-with-docker) is the full single-host guide.

### Kubernetes

[`deploy/kubernetes/`](deploy/kubernetes/deployment.yaml) holds the API Deployment, the migrate
Job and the CronJobs for the dispatcher and the media cleanup;
[`docs/deployment.md`](docs/deployment.md#kubernetes) explains them.

DEBUG, the demo seed and the repository root's `compose.yaml` are for development only.

## Authentication and tenant scope

Every `/api/v1` call except health carries a service key and a tenant:

| Header | Required | Meaning |
| --- | --- | --- |
| `Authorization: Bearer <key>` | yes | a service key (`ocr_...`) |
| `X-Tenant-ID` | yes | the tenant the key is granted |
| `X-Namespace-Type`, `X-Namespace-ID` | both or neither | an optional namespace inside the application |
| `X-Actor-ID` | no | who acted, recorded in the audit log |

Create a key with `create_service_token`. It prints the key once; `--json` prints one JSON
object. From `deploy/docker/` in production (in the development stack, the service is
`octoreview` instead of `app`):

```sh
docker compose exec app python manage.py create_service_token --name storefront \
  --tenant example --application storefront --namespace-wildcard \
  --scope reviews:read --scope reviews:write --scope media:write --scope entities:read
```

Those four scopes cover every [API example](#api-examples). Smaller keys for each job are in
[spec section 2](docs/spec.md#2-authentication-and-authorization).

The service key stays on your backend. Your backend sets `author_id` from its own signed-in user
([spec section 1](docs/spec.md#1-scoping-and-identity)), and only the signed upload URL from a
media intent goes to the browser.

## API examples

The base path is `/api/v1`. Each block reads `.octoreview-dev.env`, so it runs as written after
the quickstart.

Create a review (the entity is created on first use):

```sh
. ./.octoreview-dev.env
curl -sS -X POST "$OCTOREVIEW_BASE_URL/api/v1/reviews" \
  -H "Authorization: Bearer $OCTOREVIEW_API_KEY" \
  -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID" \
  -H "Content-Type: application/json" \
  -d "{\"application_id\": \"$OCTOREVIEW_APPLICATION_ID\", \"entity_type\": \"product\",
       \"external_id\": \"sku-quickstart\", \"author_id\": \"readme-$(date +%s)-$RANDOM\",
       \"rating\": 4, \"title\": \"Good fit\", \"body\": \"Runs a size small.\"}"
```

Get the rating summaries of up to 100 entities by your own ids:

```sh
. ./.octoreview-dev.env
curl -sS -X POST "$OCTOREVIEW_BASE_URL/api/v1/entities/ratings/query" \
  -H "Authorization: Bearer $OCTOREVIEW_API_KEY" \
  -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID" \
  -H "Content-Type: application/json" \
  -d "{\"application_id\": \"$OCTOREVIEW_APPLICATION_ID\", \"entity_type\": \"product\", \"external_ids\": [\"sku-quickstart\"]}"
```

List an entity's published reviews, newest first:

```sh
. ./.octoreview-dev.env
curl -sS "$OCTOREVIEW_BASE_URL/api/v1/reviews?application_id=$OCTOREVIEW_APPLICATION_ID&entity_type=product&external_id=sku-quickstart&limit=5" \
  -H "Authorization: Bearer $OCTOREVIEW_API_KEY" \
  -H "X-Tenant-ID: $OCTOREVIEW_TENANT_ID"
```

A photo or video takes three steps; [`scripts/quickstart.sh`](scripts/quickstart.sh) runs them
with curl, and CI runs it:

```text
your backend / browser             octoreview                          storage
        |                              |                                  |
        |  1. POST /reviews/{id}/media |                                  |
        |  (kind, content_type, size)  |                                  |
        |----------------------------->|  media `pending` + signed URL    |
        |<-----------------------------|                                  |
        |  2. PUT the bytes to the signed URL --------------------------->|
        |  3. POST .../media/{media_id}/complete                          |
        |----------------------------->|  checks size, type, signature -->|
        |<-----------------------------|  media `ready` with a read URL   |
```

Errors use one envelope (`error.code`, `message`, `details`, `request_id`, `doc_url`,
`retryable`; [spec section 11](docs/spec.md#11-errors)), and each code has a section in
[`docs/errors.md`](docs/errors.md). Lists use limit and offset with a `{data, pagination}`
envelope ([spec section 5](docs/spec.md#5-rest-api-apiv1)). The full contract is
[`docs/openapi.yaml`](docs/openapi.yaml), and [`docs/events.md`](docs/events.md) lists every
webhook event.

## SDKs

The SDKs are for server code: they hold a service key.

| Language | Install | Repository |
| --- | --- | --- |
| Go | `go get github.com/octoverse-id/octoreview-go` | [octoreview-go](https://github.com/octoverse-id/octoreview-go) |
| PHP | `composer require octoverse-id/octoreview-php` | [octoreview-php](https://github.com/octoverse-id/octoreview-php) |
| JavaScript | `npm install @octoverse-id/octoreview` | [octoreview-js](https://github.com/octoverse-id/octoreview-js) |

Each SDK's `examples/quickstart` reads the same `.octoreview-dev.env`. From an SDK checkout next
to this one:

```sh
go run ./examples/quickstart ../octoreview/.octoreview-dev.env                       # octoreview-go
composer install && php examples/quickstart.php ../octoreview/.octoreview-dev.env    # octoreview-php
make build && node examples/quickstart.mjs ../octoreview/.octoreview-dev.env         # octoreview-js
```

The SDKs are still 0.x while the server is stable; [`docs/versioning.md`](docs/versioning.md)
has the compatibility table.

## Configuration

[`.env.example`](.env.example) lists every setting with a comment. A first deployment sets:

- `DJANGO_SECRET_KEY`, `OCTOREVIEW_SERVICE_TOKEN_PEPPER` and `OCTOREVIEW_ERASURE_HMAC_KEY`;
- `DATABASE_URL` and `DJANGO_ALLOWED_HOSTS`;
- `OCTOREVIEW_STORAGE_BACKEND`, `OCTOREVIEW_STORAGE_LOCATION_ID` and the backend's own settings
  ([`docs/storage.md`](docs/storage.md));
- `OCTOREVIEW_OUTBOX_TRANSPORT` and, for `webhook`, its URL and secret.

With DEBUG off, the server refuses to start while a required setting is missing, and the error
names the setting and the fix.

## Common commands

```sh
make help            # every target
make check           # the fast gate: no Docker, no network
make test            # the test suite on SQLite
make test-postgres   # the test suite on PostgreSQL (needs Docker)
make release-check   # the full gate (needs Docker)
make storage-check   # round-trips a probe object through the backend your environment configures
```

## Documentation

- [Spec of record](docs/spec.md): what the service does, one value per item; wins over every other doc
- [Agent rules](AGENTS.md): product rules, gates and the development pipeline
- [Architecture](docs/architecture.md): layer map, module ownership and octonomy copy provenance
- [OpenAPI contract](docs/openapi.yaml): every `/api/v1` operation, request and response schema
- [Error codes](docs/errors.md): one section per error code with status, details, fix and recovery
- [Events](docs/events.md): the outbox envelope, webhook signature and every event payload
- [SDK surface](docs/sdk-surface.yaml): each operation's Go, PHP and JS method ([table](docs/sdk-surface.md))
- [Compose stack](compose.yaml): the development stack (`make up`, `make up-s3`, `make quickstart`)
- [Operations](docs/operations.md): metrics, alerts, scheduled jobs, failure playbooks, secret rotation
- [Deployment](docs/deployment.md): the image, gunicorn, migrations, reverse proxy, Kubernetes and single host
- [Kubernetes manifests](deploy/kubernetes/deployment.yaml): the API Deployment, the migrate Job and the CronJobs
- [Single-host compose](deploy/docker/compose.yaml): production compose with migrate, dispatcher and cleanup
- [Storage](docs/storage.md): backends, the S3 IAM policy, bucket CORS and lifecycle, compatible stores
- [Environment](.env.example): every setting with a comment
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Release runbook](docs/release.md)
- [Versioning policy](docs/versioning.md)
- [Rulesets](.github/rulesets/README.md): the live rulesets' reviewable mirror
- [TODOS](TODOS.md): deferred work, each with a trigger
- [llms.txt](llms.txt): the same index for agents

## Contributing, security and license

Contributions are welcome: read [`CONTRIBUTING.md`](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as
[`SECURITY.md`](SECURITY.md) describes, never in a public issue.

Octoreview is licensed under the [Apache License 2.0](LICENSE); see [`NOTICE`](NOTICE).
