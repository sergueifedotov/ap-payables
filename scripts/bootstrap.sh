#!/usr/bin/env bash
# SPIRE server and agent, three workload entries, Vault, Postgres, gateway.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
TD="ap.example"
NODE_ID="spiffe://${TD}/node/laptop"

server() { docker compose exec -T spire-server /opt/spire/bin/spire-server "$@" -socketPath /opt/spire/sockets/server/api.sock; }

if docker ps --format '{{.Ports}}' | grep -q ':8201->'; then
  echo "Port 8201 is already published. Stop the other stack, then rerun." >&2
  exit 1
fi

echo "0. Clearing any previous run (containers, volumes, .state)."
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
rm -rf .state .env
mkdir -p .state/bootstrap .state/out
chmod 777 .state .state/out

echo "1. Building the gateway image ..."
docker compose build gateway

echo "2. Starting SPIRE server (trust domain ${TD}) ..."
JOIN_TOKEN=pending docker compose up -d --wait spire-server

echo "3. Exporting the server's CA bundle for the agent's first connection."
server bundle show > .state/bootstrap/bundle.pem

echo "4. Minting a one-time join token for the node ${NODE_ID} ..."
TOKEN="$(server token generate -spiffeID "${NODE_ID}" -ttl 600 | awk '/Token:/ {print $2}')"
if [[ -z "${TOKEN}" ]]; then
  echo "Could not mint a join token." >&2
  exit 1
fi
echo "JOIN_TOKEN=${TOKEN}" > .env

echo "5. Starting SPIRE agent, Postgres, and Vault."
docker compose up -d spire-agent postgres vault
for _ in $(seq 1 40); do
  if docker compose exec -T spire-agent /opt/spire/bin/spire-agent healthcheck \
       -socketPath /opt/spire/sockets/agent/api.sock >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T spire-agent /opt/spire/bin/spire-agent healthcheck \
  -socketPath /opt/spire/sockets/agent/api.sock

echo "6. Registering three workloads. Selector = the Unix uid the agent sees."
server entry create -parentID "${NODE_ID}" -spiffeID "spiffe://${TD}/workload/ap-agent" \
  -selector unix:uid:1001 -jwtSVIDTTL 120 >/dev/null
server entry create -parentID "${NODE_ID}" -spiffeID "spiffe://${TD}/workload/notes-bot" \
  -selector unix:uid:1002 -jwtSVIDTTL 120 >/dev/null
server entry create -parentID "${NODE_ID}" -spiffeID "spiffe://${TD}/workload/pay-gateway" \
  -selector unix:uid:1003 -jwtSVIDTTL 120 >/dev/null
server entry show

echo "   Waiting until uid 1003 can fetch an SVID ..."
for _ in $(seq 1 30); do
  if docker compose exec -T -u 1003 spire-agent /opt/spire/bin/spire-agent api fetch jwt \
       -audience bootstrap-probe -socketPath /opt/spire/sockets/agent/api.sock >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T -u 1003 spire-agent /opt/spire/bin/spire-agent api fetch jwt \
  -audience bootstrap-probe -socketPath /opt/spire/sockets/agent/api.sock >/dev/null

echo "7. Exporting the JWT public keys Vault will trust."
server bundle show -format spiffe > .state/jwks.json
docker compose run -T --rm --no-deps --user 0 --entrypoint python3 gateway \
  /opt/app/gateway/jwk_to_pem.py /opt/app/state/jwks.json > .state/jwt-pub.pem
if ! grep -q "BEGIN PUBLIC KEY" .state/jwt-pub.pem; then
  echo "JWT public key export failed." >&2
  exit 1
fi

echo "8. Waiting for Postgres and Vault, then configuring Vault."
docker compose up -d --wait postgres vault
./scripts/setup_vault.sh

echo "9. Starting the pay gateway (uid 1003, the only Vault client)."
docker compose up -d --wait gateway
echo "Bootstrap done. Next: ./scripts/demo.sh"
