# Versioning policy

Octoreview follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## Release line policy

The server shipped `1.0.0-rc.N` tags (`rc.2` to `rc.4`; `rc.1` was never tagged), then `1.0.0`.
The owner cut `1.0.0` on 2026-11-26 on the CI evidence (the storage emulators and each SDK's `s3`
integration run) instead of waiting for a production integration on a real `s3` account, so AWS
S3 is "beta" in `1.0.0` until an operator's run passes (`docs/release.md`, "Operator smoke
runs"). Release candidates publish immutable image tags without moving `latest` or stable
aliases.

## Image tags

`ghcr.io/octoverse-id/octoreview` (`docs/release.md` describes how each is published):

| Tag | Moves | Published by |
| --- | --- | --- |
| `X.Y.Z` | never | a `vX.Y.Z` tag |
| `X.Y.Z-rc.N` | never | a `vX.Y.Z-rc.N` tag; candidates move no other tag |
| `X.Y` | to the newest `X.Y.x` release | a `vX.Y.Z` tag that is the newest in its line |
| `latest` | to the newest release | a `vX.Y.Z` tag that is the newest overall; candidates never move it |
| `edge` | to main's tip | every green CI run on `main` (amd64, unattested, not for production) |
| `sha-<commit>` | never | the same run as `edge`; SDK CI pins one of these |

## Rules

- Breaking changes are not allowed on `/api/v1` after `1.0.0`; additive changes are minor.
- The OpenAPI contract (`docs/openapi.yaml`) and the event catalog are frozen in release
  line 1 for every storage backend and the full SDK surface, so line 2 (Azure Blob, GCS) adds
  no breaking change.
- Deprecated fields and methods keep working, with a warning, for at least one minor release.
- Version bumps and tags happen only in a `release/<version>` PR (see `docs/release.md`).
