# The gateway may mint one dynamic Postgres role and revoke the lease it just received.
# sys/leases/revoke is wider than a production policy: a caller who already knows
# some other lease id can revoke that too.

path "database/creds/ap-invoice-match" {
  capabilities = ["read"]
}

path "sys/leases/revoke" {
  capabilities = ["update"]
}

path "sys/leases/revoke/*" {
  capabilities = ["update"]
}
