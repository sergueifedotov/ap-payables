#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0
run() {
  echo
  echo "=== $2 (uid $1) ==="
  if ./scripts/call.sh "$1" "$2"; then
    :
  else
    fail=1
  fi
}
run 1001 pay
run 1001 over
run 1001 blocked
run 1001 staff
run 1001 engineering
run 1002 notes
run 1001 passthrough
run 1001 vault-direct
echo
if [[ "${fail}" -eq 0 ]]; then
  echo "demo: all modes passed"
else
  echo "demo: a mode failed" >&2
fi
exit "${fail}"
