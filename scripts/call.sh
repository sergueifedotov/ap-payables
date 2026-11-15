#!/usr/bin/env bash
# Run the accounts-payable agent as a Unix uid.
# The exec'd process does not receive the on-behalf-of signing key.
#   scripts/call.sh 1001 pay
#   scripts/call.sh 1001 --user alice --group accounts-payable --question "Can we pay invoice 4411?"
#   scripts/call.sh 1002 notes
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -lt 2 ]]; then
  echo "usage: call.sh <uid> <pay|over|blocked|staff|engineering|notes|passthrough|vault-direct>" >&2
  echo "       call.sh <uid> --user NAME [--group GROUP ...] [--audience AUD] --question TEXT" >&2
  echo "       call.sh <uid> --user NAME [--group GROUP ...] --passthrough" >&2
  echo "       call.sh <uid> --vault" >&2
  exit 1
fi
UID_ARG="$1"
shift
exec docker compose exec -T -u "${UID_ARG}" \
  -e OBO_JWT_SECRET=client-does-not-hold-this \
  gateway python3 -u /opt/app/gateway/agent.py "$@"
