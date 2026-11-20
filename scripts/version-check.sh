#!/usr/bin/env bash
# The version stamps of a release must agree (make version-check; docs/release.md). The
# publish workflow runs it for every vX.Y.Z and vX.Y.Z-rc.N tag; a release PR makes it pass.
#
#   pyproject.toml   version = "1.0.0rc1"   (PEP 440; compared as semver 1.0.0-rc.1)
#   docs/openapi.yaml info.version          (the contract the SDKs vendor)
#   config/settings.py SPECTACULAR_SETTINGS "VERSION"
#   CHANGELOG.md     a "## [1.0.0-rc.1]" heading
#   deploy/          every ghcr.io/octoverse-id/octoreview image tag
#
# Usage: scripts/version-check.sh [REPO_DIR]   Exit codes: 0 agree | 1 disagree | 2 unreadable
set -euo pipefail
cd "${1:-$(dirname "$0")/..}"

read_one() {
  local label=$1 value=$2
  [ -n "$value" ] || { echo "version-check: could not read the $label version" >&2; exit 2; }
  printf '%s' "$value"
}

pep440=$(read_one pyproject "$(sed -nE 's/^version = "([^"]+)"$/\1/p' pyproject.toml | head -n1)")
semver=$(printf '%s' "$pep440" | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+)rc([0-9]+)$/\1-rc.\2/')
openapi=$(read_one openapi "$(awk '/^info:/{i=1; next} i && /^  version:/{print $2; exit} /^[^ ]/{i=0}' docs/openapi.yaml)")
settings=$(read_one settings "$(sed -nE 's/^ *"VERSION": "([^"]+)",?$/\1/p' config/settings.py | head -n1)")

echo "pyproject=$pep440 (semver $semver) openapi=$openapi settings=$settings"
status=0
[ "$openapi" = "$semver" ] || { echo "FAIL  docs/openapi.yaml info.version is $openapi, not $semver"; status=1; }
[ "$settings" = "$semver" ] || { echo "FAIL  config/settings.py VERSION is $settings, not $semver"; status=1; }
grep -qF "## [$semver]" CHANGELOG.md || { echo "FAIL  CHANGELOG.md has no \"## [$semver]\" heading"; status=1; }
if [ -d deploy ]; then
  while IFS= read -r ref; do
    [ "${ref##*:}" = "$semver" ] || { echo "FAIL  deploy/ references $ref, not $semver"; status=1; }
  done < <(grep -rhoE 'ghcr\.io/octoverse-id/octoreview:[^"[:space:]]+' deploy | sort -u)
fi
[ "$status" = 0 ] && echo "version-check OK ($semver)"
exit "$status"
