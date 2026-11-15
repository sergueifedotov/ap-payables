# Demo

Read [Plain English](./PLAIN_ENGLISH.md) for the business call. This page is the run. [Step by step](./STEP_BY_STEP.md) is what the running stack printed, line by line.

**Prereq:** Docker running, `jq`, and Ollama with `qwen2.5:7b` and `nomic-embed-text`. Port 8201 has to be free. Vault in this project is published on 8201 so a dev Vault on 8200 can stay up. From this directory:

```bash
python3 -m gateway.check_rules
./scripts/bootstrap.sh
```

**Expect from the checks:** `PASS payables checks`. They cover the payment clause, cosine ranking, a score under 0.50 returning no passage, a live embedding search when Ollama is up, invoices 4411, 4412, and 4413, and the exchange. PyJWT is the dependency they need.

**Expect from bootstrap:** three SPIFFE IDs under `spiffe://ap.example/node/laptop` (`ap-agent` uid 1001, `notes-bot` uid 1002, `pay-gateway` uid 1003), then `engine check: minted v… and revoked …`, then a healthy gateway.

Admin UI, optional: http://localhost:8201 token `root`. Dev only.

```bash
./scripts/demo.sh
```

Or one mode:

```bash
./scripts/call.sh 1001 pay
```

## Asking without a mode

A mode is a saved call, and it ends with `PASS` or `FAIL`. The flags are that same call with the person, the group, and the question written out. They print the answer or the refusal. They do not print `PASS`.

The uid is still the first argument of `call.sh`. SPIRE names the program from that uid. `--audience` changes only the audience on the workload JWT. The user assertion is always minted for `pay-gateway`.

```bash
./scripts/call.sh 1001 --user alice --group accounts-payable \
  --question "Can we pay invoice 4411?"
```

| You want | Flags | What happens |
|----------|-------|----------------|
| The same question as `pay`, `over`, or `blocked` | `--user alice --group accounts-payable --question "Can we pay invoice NNNN?"` | Exchange, then the model. A question that contains `invoice` uses both tools |
| The same question as `staff` | `--user alice --group accounts-payable --question "What is staff compensation?"` | Exchange, then the model. A question with no invoice searches policy only and does not read the ledger |
| The same refusal as `engineering` | `--user bob --group engineering --question "Can we pay invoice 4411?"` | Exchange HTTP 400. The model is not called |
| A caller with no group | `--user alice --question "Can we pay invoice 4411?"` | Exchange HTTP 400. Omit `--group` and the assertion carries no group |
| The same refusal as `notes` | uid `1002`, with Alice's flags | SPIRE issues `notes-bot`. Exchange HTTP 400, allowlist |
| A workload token for the wrong audience | `--audience mail-tool` added to Alice's flags | Exchange HTTP 400. `--audience` does not change who the program is |
| The same refusal as `passthrough` | `--user alice --group accounts-payable --passthrough` | No exchange. `match_invoice` for 4411 is called with the assertion. HTTP 403 |
| The same refusal as `vault-direct` | `--vault` | No gateway. The process posts its SPIFFE JWT to Vault. With uid 1001 the subject is the AP agent and Vault refuses it |

```bash
./scripts/call.sh 1001 --user bob --group engineering --question "Can we pay invoice 4411?"
./scripts/call.sh 1001 --user alice --group accounts-payable --passthrough
./scripts/call.sh 1001 --vault
```

## What each mode prints

**pay.** Exchange HTTP 200. The model names `search_policy` and `match_invoice`. `agent.py` sends those MCP calls. The match JSON has `payable` true, vendor `northwind`, amount `9800`, `on_behalf_of` alice, `db_application_name` alice. The model's own paragraph mentions 4411 and the amount 9800, and does not say do not pay. Then `PASS`.

**over.** Invoice 4412. `payable` false. The recommendation says `amount exceeds the remaining balance` and still quotes the clause. `PASS`.

**blocked.** Invoice 4413. The recommendation says `vendor is not approved` and still quotes the clause. `PASS`.

**staff.** "What is staff compensation?" Exchange HTTP 200. `search_policy` returns no passages and says this corpus does not include payroll. The model's paragraph says the question is outside accounts payable. `corpus/payroll/` is not opened. `PASS`.

**engineering.** Bob, group engineering, uid 1001. Exchange HTTP 400, `accounts-payable`. No MCP call. `PASS`.

**notes.** Alice's assertion, uid 1002. Exchange HTTP 400, `allowlist`. `PASS`.

**passthrough.** No exchange. `tools/call` returns HTTP 403 and `on-behalf-of`. `PASS`.

**vault-direct.** The AP agent's JWT is posted to Vault `auth/jwt/login`, role `pay-gateway`. Status 400 or higher, and the body mentions `sub`. `PASS`.

## The role is gone

```bash
./scripts/call.sh 1001 pay
docker compose exec -T postgres psql -U payables -d payables -c \
  "SELECT count(*) FROM pg_roles WHERE rolname = '$(tr -d '\n' < .state/last_role.txt)'"
```

**Expect:** a count of `0`. `.state/last_role.txt` is the operator's note. The match JSON the agent printed does not contain that name.

## Stop

`./scripts/down.sh` removes containers, volumes, the join token, and `.state/`.
