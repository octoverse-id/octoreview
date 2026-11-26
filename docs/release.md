# Release runbook

Releases of octoreview are cut from `main` through a dedicated PR. This runbook grows with the
release workflow; until the first release it records the invariants every release keeps.

## Invariants

- A release PR is named `release/<version>` and only bumps the version and moves the
  `[Unreleased]` CHANGELOG entries under the new version heading. The version lives in four
  places that `make version-check` keeps in step: `pyproject.toml` (PEP 440, for example
  `1.0.0rc1`), `docs/openapi.yaml` `info.version`, `SPECTACULAR_SETTINGS["VERSION"]` in
  `config/settings.py`, and the CHANGELOG heading (semver, `1.0.0-rc.1`).
- The tag `v<version>` is created on the merge commit of that PR after its CI is green.
- Release tags never move: the `release tags: never move` ruleset rejects re-pointing or
  force-pushing a `v*` tag, admins included. Only repo admins can delete a wrong tag
  (`release tags: deletion is admin-only`), and the version is then re-cut, not re-used.
- `make release-check` must pass on the release commit.
- Octoreview owns no cloud account. Before a tag, the storage gates are the emulator runs in CI
  (`storage-emulators`, `compose-quickstart` on the `s3` profile, each SDK's `s3` integration
  run). Real-cloud evidence comes from operators (see [Operator smoke runs](#operator-smoke-runs)).

## Steps

1. Open an issue for the release and a `release/<version>` branch from `main`.
2. Bump the version and update `CHANGELOG.md`; open the PR with `Closes #<issue-number>`.
3. Merge when CI is green, then tag the merge commit `v<version>` (`v1.0.0-rc.1`, `v1.0.0`)
   and push the tag.
4. The tag starts `.github/workflows/publish-image.yml` (job `release`). Before building, it
   refuses a commit that is not on main, has no green CI run on main, is not the release merge
   (`scripts/check-release-merge.sh`), has disagreeing version stamps, or names a version
   already published. It then builds amd64 and arm64 by digest, smoke-tests both (the full
   `docker-smoke` on amd64), attests provenance and an SBOM per platform, verifies them, checks
   an anonymous pull and promotes the tags (`docs/versioning.md`). A failed run is re-run, never
   re-tagged: a version that is already published is re-promoted from its existing digest.

## Operator smoke runs

An operator is anyone running Octoreview on their own infrastructure. A backend leaves "beta" in
`docs/storage.md` when one operator run on a real account passes (`spec.md` section 19). The
operator runs these in their own deployment, against their own bucket, with their own credentials:

1. `manage.py check_storage` from a pod or host that reaches the bucket's public endpoint.
2. `scripts/smoke-upload.sh` with a key for a dedicated smoke application
   (`docs/operations.md`, "Smoke test").
3. The browser hand-off: an upload from the storefront origin through the presigned URL, which
   proves the bucket's CORS rule.

The operator reports each step's outcome and its request ids in a GitHub issue (the bug report
form will do). Keys and presigned URLs are never sent or recorded. A maintainer then changes the
backend's status in `docs/storage.md` in a normal PR that links that issue. `1.0.0` did not wait
for such a run (`docs/versioning.md`).

## Rehearsing a release

Run the workflow by hand from `main` (Actions, "Publish image", `dry_run` on). It runs the whole
release pipeline (multi-arch build, both smoke tests, attestation, verification, the tag
calculation) but promotes only `:dryrun`. It skips the two checks that only a tag can satisfy:
the release merge and the version stamps.

## Every merge to main

When CI passes on a push to `main`, the `edge` job publishes `:edge` and `:sha-<commit>` (amd64,
unattested, unsupported for production). It refuses a commit that is no longer main's tip, and
it never runs for a pull request's CI, a fork's or another branch's.

## The first publish

The first `edge` run (2026-11-19, after #24) created `ghcr.io/octoverse-id/octoreview` linked to
this public repository, and it was publicly pullable at once. If a package is ever created
private (GHCR's default for packages not linked to a public repository), the anonymous pull
check fails; an org admin makes the package public (package settings, "Change visibility") and
re-runs the job.

## Who can publish

Every check in the workflow guards mistakes, not someone with write access: GitHub loads a
workflow from the ref that triggered it, so anyone who can push a tag can push one whose
workflow drops a check. Write access to this repository is publish access; the tag rulesets
(`release tags: never move`, deletion admin-only) are the controls outside the workflow.
