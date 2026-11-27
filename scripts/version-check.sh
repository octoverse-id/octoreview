#!/usr/bin/env bash
# The version stamps of a release must agree (make version-check; docs/release.md). The
# publish workflow runs it for every vX.Y.Z and vX.Y.Z-rc.N tag; a release PR makes it pass.
#
#   pyproject.toml   version = "1.0.0rc1"   (PEP 440; compared as semver 1.0.0-rc.1)
#   docs/openapi.yaml info.version          (the contract the SDKs vendor)
#   config/settings.py SPECTACULAR_SETTINGS "VERSION"
#   CHANGELOG.md     a "## [1.0.0-rc.1]" heading
#   deploy/          every ghcr.io/octoverse-id/octoreview image tag
#   README.md        every image tag and the `--branch v<version>` clone of the install steps:
#                    a final release names itself; a release candidate names the newest final
#                    "## [X.Y.Z]" heading in CHANGELOG.md, so main keeps pointing operators at the
#                    last stable release (skipped, with a note, before the first final release)
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

# One tag pattern for every reference: it ends on a letter or digit, so a closing backtick, quote
# or sentence period ("...octoreview:1.0.0.") is never part of the tag.
tag='[0-9A-Za-z_.-]*[0-9A-Za-z]'

# stale_refs LABEL EXPECTED PATH...: a FAIL line for every image reference or `--branch v<tag>`
# clone in PATH that does not name EXPECTED. It names LABEL when one is given, else file:line.
stale_refs() {
  local label=$1 expected=$2 file line ref found
  shift 2
  while IFS=: read -r file line ref; do
    case $ref in
      --branch\ v*) found=${ref#--branch v} ;;
      *) found=${ref##*:} ;;
    esac
    [ "$found" = "$expected" ] || echo "FAIL  ${label:-$file:$line} references $ref, not $expected"
  done < <(grep -rHnoE -e "ghcr\.io/octoverse-id/octoreview:$tag" -e "--branch v$tag" "$@")
}
report() { [ -z "$1" ] || { printf '%s\n' "$1"; status=1; }; }

[ -d deploy ] && report "$(stale_refs deploy/ "$semver" deploy | sort -u)"

if [ "${semver#*-}" = "$semver" ]; then
  readme=$semver
else
  # A release candidate leaves the README on the last final release.
  readme=$(sed -nE 's/^## \[([0-9]+\.[0-9]+\.[0-9]+)\].*/\1/p' CHANGELOG.md | head -n1)
  if [ -n "$readme" ]; then
    echo "NOTE  release candidate: README.md must name the last final release, $readme"
  else
    echo "NOTE  README.md not checked: CHANGELOG.md has no final release yet"
  fi
fi
if [ -n "$readme" ]; then
  [ -r README.md ] || { echo "version-check: could not read README.md" >&2; exit 2; }
  report "$(stale_refs "" "$readme" README.md)"
  if [ "$readme" = "$semver" ]; then
    if ! grep -qE "ghcr\.io/octoverse-id/octoreview:$tag" README.md; then
      report "FAIL  README.md has no image reference"
    elif ! grep -qE -e "--branch v$tag" README.md; then
      report "FAIL  README.md has no --branch v$semver clone"
    fi
  fi
fi
[ "$status" = 0 ] && echo "version-check OK ($semver)"
exit "$status"
