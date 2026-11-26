#!/usr/bin/env bash
# Full verification for the Volcano tutorial.
# Usage: bash scripts/verify_all.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MINI="$ROOT/mini-volcano"
FAILED=0

section() {
  printf '\n== %s ==\n' "$1"
}

check() {
  if "$@"; then
    printf 'PASS %s\n' "$*"
  else
    printf 'FAIL %s\n' "$*"
    FAILED=1
  fi
}

section "gofmt"
UNFORMATTED="$(cd "$MINI" && gofmt -l .)"
if [ -n "$UNFORMATTED" ]; then
  printf 'FAIL unformatted files:\n%s\n' "$UNFORMATTED"
  FAILED=1
else
  printf 'PASS gofmt\n'
fi

section "go vet"
if (cd "$MINI" && go vet ./...); then printf 'PASS go vet\n'; else printf 'FAIL go vet\n'; FAILED=1; fi

section "go build"
if (cd "$MINI" && go build ./...); then printf 'PASS go build\n'; else printf 'FAIL go build\n'; FAILED=1; fi

section "go test"
if (cd "$MINI" && go test ./... -count=1); then printf 'PASS go test\n'; else printf 'FAIL go test\n'; FAILED=1; fi

section "test counts"
TOP="$(cd "$MINI" && go test ./... -count=1 -v 2>&1 | grep -c '^--- PASS')"
NESTED="$(cd "$MINI" && go test ./... -count=1 -v 2>&1 | grep -c '^    --- PASS')"
TOTAL=$((TOP + NESTED))
printf 'top-level=%s nested=%s total=%s\n' "$TOP" "$NESTED" "$TOTAL"
if [ "$TOP" -ne 129 ] || [ "$NESTED" -ne 12 ] || [ "$TOTAL" -ne 141 ]; then
  printf 'FAIL expected 129 top-level + 12 nested = 141\n'
  FAILED=1
else
  printf 'PASS 141 PASS lines\n'
fi

section "scenarios"
if (cd "$MINI" && go run ./cmd/minivolcano -all); then
  printf 'PASS scenarios\n'
else
  printf 'FAIL scenarios\n'
  FAILED=1
fi

section "docs and examples"
if (cd "$ROOT" && python scripts/check_docs.py >/tmp/volcano-check-docs.log 2>&1); then
  tail -n 3 /tmp/volcano-check-docs.log
  printf 'PASS check_docs.py\n'
else
  cat /tmp/volcano-check-docs.log
  printf 'FAIL check_docs.py\n'
  FAILED=1
fi

section "capacity model"
if (cd "$ROOT" && python scripts/capacity_model.py --selftest >/tmp/volcano-capacity.log 2>&1); then
  tail -n 1 /tmp/volcano-capacity.log
  printf 'PASS capacity model selftest\n'
else
  cat /tmp/volcano-capacity.log
  printf 'FAIL capacity model selftest\n'
  FAILED=1
fi

section "race (optional)"
CGO="$(cd "$MINI" && go env CGO_ENABLED)"
if [ "$CGO" = "1" ]; then
  if (cd "$MINI" && go test -race ./internal/scenario ./internal/actions -count=1); then
    printf 'PASS race\n'
  else
    printf 'FAIL race\n'
    FAILED=1
  fi
else
  printf 'SKIP -race: CGO_ENABLED=0\n'
fi

section "summary"
if [ "$FAILED" -ne 0 ]; then
  printf 'VERIFICATION FAILED\n'
  exit 1
fi
printf 'VERIFICATION PASSED\n'
