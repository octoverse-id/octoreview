# Octoreview

Octoreview stores reviews, ratings and review media (images and videos) for any
external entity, scoped by tenant, application and optional namespace. Media limits are
configurable per entity, and media bytes go to a configurable storage backend: local
filesystem, AWS S3 and S3-compatible stores, Azure Blob Storage or Google Cloud Storage.

Official SDKs: [Go](https://github.com/octoverse-id/octoreview-go),
[PHP](https://github.com/octoverse-id/octoreview-php) and
[JavaScript](https://github.com/octoverse-id/octoreview-js).

## Status

Bootstrapped: governance files, rulesets and a CI skeleton whose placeholder jobs become real
jobs as the code lands. No release yet. Release line 1 ships the server (local and S3 storage)
with the Go, PHP and JavaScript SDKs; see [`docs/versioning.md`](docs/versioning.md).

## Working on this repository

Read [`AGENTS.md`](AGENTS.md) first (humans too): it holds the rules, gates and development
pipeline. `make help` lists the targets; `make check` is the fast gate.

## License

[Apache License 2.0](LICENSE).
