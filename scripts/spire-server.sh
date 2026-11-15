#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose exec -T spire-server /opt/spire/bin/spire-server "$@" \
  -socketPath /opt/spire/sockets/server/api.sock
