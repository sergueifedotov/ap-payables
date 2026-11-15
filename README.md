# Accounts payable

Alice in accounts payable asks whether invoice 4411 can be paid. The answer has two sources, and they are not interchangeable.

The **policy** is a document. Retrieval returns the clause: pay only when the vendor is approved, the invoice cites an open purchase order, and the amount does not exceed that order's remaining balance. The **ledger** is Postgres: vendors, purchase orders, and invoices. The amounts come from there.

Ollama runs two models. `qwen2.5:7b` names the tool and writes the recommendation. It does not memorize the clause and does not write SQL. `nomic-embed-text` embeds the query and the policy files. `agent.py`, which is uid 1001, sends the MCP call with Alice's exchanged token and the AP agent's SPIFFE JWT. Ollama does not receive those credentials. `search_policy` returns the nearest passages at or above a similarity of 0.50. `match_invoice` reads the ledger.

SPIFFE names the program: uid 1001 is the AP agent, uid 1002 is the notes bot, uid 1003 is the gateway. On-behalf-of turns Alice's assertion into a token whose subject is Alice, whose actor is the AP agent, and whose audience is `ap-payables`. Vault logs in only as the gateway, mints a Postgres role for that one match, names the session Alice, and revokes the role before the tool result returns.

This stack publishes Vault on host port **8201**. A dev Vault already using 8200 can stay up.

## Run

From this directory. Docker, `jq`, and Ollama are required. Ollama needs `qwen2.5:7b` and `nomic-embed-text`. Port 8201 has to be free.

```bash
python3 -m gateway.check_rules
./scripts/bootstrap.sh
./scripts/demo.sh
./scripts/down.sh
```

| Mode | Uid | Business result |
|------|-----|-----------------|
| `pay` | 1001 | Invoice 4411, Northwind, 9800 against PO 900. Pay. The answer quotes the policy clause |
| `over` | 1001 | Invoice 4412 is 12000 on a PO with 10000 remaining. Do not pay |
| `blocked` | 1001 | Invoice 4413 is Contoso, and Contoso is blocked. Do not pay |
| `staff` | 1001 | "What is staff compensation?" The nearest accounts-payable passage is below the similarity floor, so the tool returns no passage. Payroll is not opened |
| `engineering` | 1001 | Bob is not in accounts payable. No tool token |
| `notes` | 1002 | The notes bot is a valid SPIFFE ID and is not the AP agent |
| `passthrough` | 1001 | Alice's original assertion is refused by the tools |
| `vault-direct` | 1001 | The agent's SPIFFE JWT is refused by Vault |

```bash
./scripts/call.sh 1001 pay
./scripts/call.sh 1001 --user alice --group accounts-payable \
  --question "Can we pay invoice 4411?"
```

The mode ends with `PASS` or `FAIL`. The flags are the same call with the person, the group, and the question written out, and they print the answer without a `PASS` line. The uid still selects the program. The cases are in [Demo](docs/DEMO.md).

## What each piece is for

| Piece | Business job |
|-------|----------------|
| Retrieval | The payment rule is a document. Embeddings rank that clause above onboarding and filing notes. Payroll files sit in another directory and are not embedded |
| MCP | The agent has `search_policy` and `match_invoice`. It has no tool that writes a payment or reads a password |
| SPIFFE | Only the AP agent program may ask. The gateway's own ID is the one Vault accepts |
| On-behalf-of | The tool token is Alice, the AP agent, and `ap-payables`. The assertion she presented was for `pay-gateway` and is refused |
| Vault | The ledger password exists for the length of `match_invoice` and is revoked before that result is returned |
| Ollama | The Mac app, not a compose service. `qwen2.5:7b` names the tool and writes the paragraph. `nomic-embed-text` embeds `corpus/ap/`. The MCP request stays with the attested agent |

`gateway/agent.py` is the model loop for `pay`, `over`, `blocked`, and `staff`. It sends the question to Ollama, sends each tool call the model names, and prints the model's answer. `engineering`, `notes`, `passthrough`, and `vault-direct` stop before the model. `search_policy` ranks passages by cosine similarity and drops a score under 0.50.

The drawings, the two Ollama models, and why the agent sends the MCP call are in [Plain English](docs/PLAIN_ENGLISH.md). The lines from a live `./scripts/demo.sh`, and what each line means, are in [Step by step](docs/STEP_BY_STEP.md).

## Ledger

| Invoice | Vendor | Purchase order | Amount | Why |
|---------|--------|----------------|--------|-----|
| 4411 | Northwind, approved | 900, remaining 10000 | 9800 | Payable |
| 4412 | Northwind, approved | 900, remaining 10000 | 12000 | Over the remaining balance |
| 4413 | Contoso, blocked | 901, remaining 5000 | 1000 | Vendor is not approved |

## Limits

Dev Vault, root token `root`, Unix uid attestation, and a local HMAC identity provider. Retrieval is one embedding model over the files in `corpus/ap/`, not a separate vector database. `sys/leases/revoke` is wider than a policy you would ship. The two-minute role is what remains if the process dies before the revoke. The payment modes depend on the local model following the tool results. A wording miss is a model miss. The ledger flag `payable` is the business fact.

SPIRE 1.15.3, Vault 1.17.6, Postgres 16, Python 3.12. MIT licensed.
