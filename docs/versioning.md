# Versioning policy

Octoreview follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## Release line policy

The server ships `1.0.0-rc.N` tags until its first consumer integration (Mbizmarket)
passes, then `1.0.0`. Release candidates publish immutable image tags without moving
`latest` or stable aliases.

## Rules

- Breaking changes are not allowed on `/api/v1` after `1.0.0`; additive changes are minor.
- The OpenAPI contract (`docs/openapi.yaml`) and the event catalog are frozen in release
  line 1 for every storage backend and the full SDK surface, so line 2 (Azure Blob, GCS) adds
  no breaking change.
- Deprecated fields and methods keep working, with a warning, for at least one minor release.
- Version bumps and tags happen only in a `release/<version>` PR (see `docs/release.md`).
