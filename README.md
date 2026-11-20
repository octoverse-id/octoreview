# Octoreview

Octoreview stores reviews, ratings and review media (images and videos) for any
external entity, scoped by tenant, application and optional namespace. Media limits are
configurable per entity, and media bytes go to a configurable storage backend: local
filesystem, AWS S3 and S3-compatible stores, Azure Blob Storage or Google Cloud Storage.

Official SDKs: [Go](https://github.com/octoverse-id/octoreview-go),
[PHP](https://github.com/octoverse-id/octoreview-php) and
[JavaScript](https://github.com/octoverse-id/octoreview-js).

## Quickstart

With Docker:

```sh
make up          # octoreview + PostgreSQL on http://localhost:8000, media on the local backend
make dev-env     # writes .octoreview-dev.env: base URL, a demo key, tenant and application
make quickstart  # creates a review with a photo and prints its id, the rating and the photo URL
```

The stack also serves the operator console at http://localhost:8000/admin/; create a login with
`docker compose exec octoreview python manage.py createsuperuser`.

`make up-s3` runs the same stack with media on an S3 emulator (RustFS on port 9000), the way a
cloud deployment stores them. `make down` stops the stack; `docker compose down -v` also drops its
data. When ports 8000 or 9000 are taken, set `OCTOREVIEW_PORT` or `OCTOREVIEW_S3_PORT`.

The stack is for development: DEBUG is on and the demo key has every scope. To run the image for
real, read [`docs/deployment.md`](docs/deployment.md) and [`docs/storage.md`](docs/storage.md).

## Status

In development; no release yet. Release line 1 ships the server (local and S3 storage) with the
Go, PHP and JavaScript SDKs; see [`docs/versioning.md`](docs/versioning.md).

## Working on this repository

Read [`AGENTS.md`](AGENTS.md) first (humans too): it holds the rules, gates and development
pipeline. `make help` lists the targets; `make check` is the fast gate.

## License

[Apache License 2.0](LICENSE).
