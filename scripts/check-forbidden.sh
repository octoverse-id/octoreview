#!/usr/bin/env bash
# Grep gates from docs/spec.md sections 12 and 13. Each rule names why it exists.
# Output on failure: one `<rule>: <file>:<line>: <text>` per hit.
set -euo pipefail
cd "$(dirname "$0")/.."
status=0

check() {
  local rule=$1 pattern=$2
  shift 2
  local hits
  hits=$(grep -rnE --include='*.py' "$pattern" "$@" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    while IFS= read -r hit; do echo "$rule: $hit"; done <<<"$hits"
    status=1
  fi
}

# octonomy's namespace rollout switches and v2 surface are not ported (spec section 13).
check "no-namespace-rollout-flags" 'NAMESPACE_WRITE_ENABLED|NAMESPACE_V2_API_ENABLED|api/v2' octoreview config
# Tests use an injected clock, never sleeps (spec section 12).
check "no-sleep-in-tests" 'time\.sleep\(' tests

# entities.ratings.apply_delta is the only writer of rating columns (spec section 4.1).
# Writes are attribute assignments or .update(...) keywords; filter(...) reads are fine.
hits=$(grep -rnE --include='*.py' --exclude-dir=migrations \
  '(\.(rating_count|rating_sum|rating_[1-5]_count|verified_review_count)\s*=[^=])|(update\(.*(rating_count|rating_sum|rating_[1-5]_count|verified_review_count)\s*=)' octoreview \
  | grep -v '^octoreview/entities/ratings.py:' | grep -v '^octoreview/entities/models.py:' || true)
if [ -n "$hits" ]; then
  while IFS= read -r hit; do echo "rating-columns-single-writer: $hit"; done <<<"$hits"
  status=1
fi

# A row lock joined to another table locks both rows unless `of=` names the table, which breaks
# the documented lock order (spec section 12).
hits=$(grep -rnE --include='*.py' --exclude-dir=migrations \
  'select_for_update\(([^o)]|o[^f])*\)\.select_related|select_related\([^)]*\)\.select_for_update\(([^o)]|o[^f])*\)' \
  octoreview || true)
if [ -n "$hits" ]; then
  while IFS= read -r hit; do echo "lock-only-own-rows: $hit"; done <<<"$hits"
  status=1
fi

# Review.ready_media_count has one writer, the lifecycle service (spec section 3.5).
hits=$(grep -rnE --include='*.py' --exclude-dir=migrations \
  '(\.ready_media_count\s*=[^=])|(update\(.*ready_media_count\s*=)' octoreview \
  | grep -v '^octoreview/lifecycle/' | grep -v '^octoreview/reviews/models.py:' || true)
if [ -n "$hits" ]; then
  while IFS= read -r hit; do echo "ready-media-count-single-writer: $hit"; done <<<"$hits"
  status=1
fi

if [ "$status" -eq 0 ]; then echo "check-forbidden OK"; fi
exit "$status"
