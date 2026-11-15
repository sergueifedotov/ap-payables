#!/usr/bin/env bash
# Enable Vault jwt auth against the SPIRE JWT key, and a dynamic Postgres role.
# The root token is used only in this script. The gateway never sees it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

v() {
  docker compose exec -T \
    -e VAULT_ADDR=http://127.0.0.1:8200 \
    -e VAULT_TOKEN=root \
    vault vault "$@"
}

echo "Writing the ap-invoice-match policy and the jwt auth method ..."
docker compose cp policies/ap-invoice-match.hcl vault:/tmp/ap-invoice-match.hcl
docker compose cp policies/creation.sql vault:/tmp/creation.sql
docker compose cp policies/revocation.sql vault:/tmp/revocation.sql
docker compose cp .state/jwt-pub.pem vault:/tmp/jwt-pub.pem

v policy write ap-invoice-match /tmp/ap-invoice-match.hcl
v auth enable jwt >/dev/null 2>&1 || true
v write auth/jwt/config "jwt_validation_pubkeys=@/tmp/jwt-pub.pem"
v write auth/jwt/role/pay-gateway \
  role_type=jwt \
  bound_audiences=vault.ap.example \
  bound_subject="spiffe://ap.example/workload/pay-gateway" \
  user_claim=sub \
  token_policies=ap-invoice-match \
  token_ttl=2m \
  token_explicit_max_ttl=2m

echo "Configuring the database secrets engine ..."
v secrets enable database >/dev/null 2>&1 || true
v write database/config/payables \
  plugin_name=postgresql-database-plugin \
  allowed_roles=ap-invoice-match \
  connection_url='postgresql://{{username}}:{{password}}@postgres:5432/payables?sslmode=disable' \
  username=vault \
  password=vault-lab-password
v write database/roles/ap-invoice-match \
  db_name=payables \
  "creation_statements=@/tmp/creation.sql" \
  "revocation_statements=@/tmp/revocation.sql" \
  default_ttl=2m \
  max_ttl=2m

echo "Proving the engine with the root token, then revoking that lease ..."
JSON="$(v read -format=json database/creds/ap-invoice-match)"
LEASE="$(jq -r .lease_id <<<"${JSON}")"
USER="$(jq -r .data.username <<<"${JSON}")"
v lease revoke "${LEASE}" >/dev/null
echo "engine check: minted ${USER} and revoked ${LEASE}"
