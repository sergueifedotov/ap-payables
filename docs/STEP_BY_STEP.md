# What a run prints

This is `./scripts/demo.sh` on the running stack, in the order the lines appear. The agent lines are what `call.sh` prints. The gateway lines are `docker compose logs gateway` for the same run. The model writes the recommendation. `agent.py` sends the MCP calls.

The page starts after the stack is up. The image build, the SPIRE join, Vault, and Postgres are `./scripts/bootstrap.sh`. Ollama is already the Mac app, with `qwen2.5:7b` and `nomic-embed-text` loaded. Those steps print nothing in the lines below. The model shows up inside the pay call: step 5 is `qwen2.5:7b` naming `match_invoice`, step 6 is the same model naming `search_policy` and the gateway embedding that query with `nomic-embed-text`, and step 9 is the paragraph the model writes.

`./scripts/call.sh` starts `agent.py` inside the gateway container as the uid you pass. The process does not receive the on-behalf-of signing key. The same lines, without a final `PASS`, come from the flag form in [Demo](./DEMO.md). `--user alice --group accounts-payable --question "Can we pay invoice 4411?"` is this pay section. A question with no invoice is the staff section. `--passthrough` and `--vault` are the last two refusals.

## Pay invoice 4411

The command is `./scripts/call.sh 1001 pay`. The question sent to the model is "Can we pay invoice 4411?"

1. `uid 1001: SPIRE issued spiffe://ap.example/workload/ap-agent aud=pay-gateway`

   The process is uid 1001. SPIRE's Unix attestor selects the registration for that uid and returns a JWT-SVID. The subject is the AP agent. The audience is `pay-gateway`, which is the audience of the exchange, not the audience of the ledger.

2. `user alice groups accounts-payable`

   The agent mints Alice's user assertion locally, signed with the identity-provider key. The subject is Alice. The group is accounts payable. The audience is `pay-gateway`. This token is not yet a tool token.

3. `exchange http 200`

   The agent posts both tokens to `POST /oauth/token`. The gateway checks the group, checks that the SPIFFE ID is the AP agent, and mints a new token signed with the on-behalf-of key. The gateway log for this line is `exchanged sub=alice act=spiffe://ap.example/workload/ap-agent aud=ap-payables`. The subject is still Alice. The actor is the AP agent. The audience is now `ap-payables`. The lifetime is two minutes. This exchange does not write an audit line.

4. `tools search_policy, match_invoice`

   The agent calls `initialize`, which does not check either token, then `tools/list`, which does. The list is those two tools. There is no tool that writes a payment or reads a password.

5. `model tool match_invoice {"invoice_id": 4411}`

   `qwen2.5:7b` chooses the first tool. The agent sends `tools/call` with Alice's exchanged token in `Authorization` and the same SPIFFE JWT in `X-Workload-SVID`. The gateway fetches its own SPIFFE JWT as uid 1003, audience `vault.ap.example`, subject `spiffe://ap.example/workload/pay-gateway`. Vault accepts that subject, mints a Postgres role that can `SELECT` the three ledger tables, and the gateway sets `application_name` to Alice. The gateway log is `match_invoice 4411 payable=True on behalf of alice; minted role v-jwt-…; lease revoked`. The role name appears in that log and in `.state/last_role.txt`. It is not in the JSON below. The audit line is `decision` allow, `vault_login` true, `payable` true.

6. `model tool search_policy {"query": "northwind payment policy"}`

   The model chooses the query. The agent sends it. The gateway asks Ollama to embed that query and each file in `corpus/ap/` with `nomic-embed-text`. `corpus/payroll/` is not opened.

7. `retrieved ['ap-payment-policy.md']`

   The payment rule is the only passage at or above a similarity of 0.50. The gateway log is `search_policy passages=1 on behalf of alice`. Vault is not called. The audit line has `vault_login` false.

8. The match JSON

   ```json
   {
     "invoice_id": 4411,
     "vendor": "northwind",
     "vendor_status": "approved",
     "po_id": 900,
     "amount": "9800",
     "po_remaining": "10000",
     "payable": true,
     "reasons": [],
     "found": true,
     "on_behalf_of": "alice",
     "db_application_name": "alice"
   }
   ```

   Northwind is approved, purchase order 900 has 10000 remaining, and the invoice is 9800, so `payable` is true. `on_behalf_of` is the subject of the exchanged token. `db_application_name` is Postgres reading back `application_name`. Vault did not supply either field. The body has no role name, lease id, or password.

9. `Pay invoice 4411. The invoice amount of $9,800 does not exceed the remaining balance of the purchase order, and the vendor is approved.`

   This sentence is the model's. The agent did not fill it in. It names invoice 4411 and the amount 9800, and it does not say do not pay.

10. `PASS invoice 4411`

    The agent checked that both tools ran, the ledger says payable, the session is Alice, and the paragraph agrees.

## Do not pay 4412

Same opening lines: uid 1001, the AP agent, Alice, exchange HTTP 200, the same two tools.

The model calls `match_invoice` for 4412, then `search_policy` with "payment policy northwind", then `match_invoice` again. Retrieval returns `ap-payment-policy.md` and `vendor-onboarding.md`. The gateway log shows two minted roles, each revoked before its result returns, and `search_policy passages=2`.

The match JSON has amount `12000`, `po_remaining` `10000`, `payable` false, and the reason `amount exceeds the remaining balance`. `on_behalf_of` and `db_application_name` are both alice.

The model writes: `Do not pay invoice 4412.` It quotes the payment clause and states that 12000 exceeds the remaining 10000. Then `PASS invoice 4412`.

## Do not pay 4413

Same opening. The model calls `match_invoice` for 4413, then `search_policy` with "policy for paying contoso", then `match_invoice` again. Retrieval returns `ap-payment-policy.md` only.

The match JSON has vendor `contoso`, `vendor_status` `blocked`, amount `1000`, `payable` false, and the reason `vendor is not approved`.

The model writes: `Do not pay invoice 4413.` It quotes the clause and says Contoso is blocked. Then `PASS invoice 4413`.

## Staff compensation

Same opening, still Alice, still the AP agent, exchange HTTP 200. The question is "What is staff compensation?"

The model calls `search_policy` with that question and does not call `match_invoice`. The agent prints `retrieved nothing`. The tool JSON is:

```json
{
  "passages": [],
  "reason": "no accounts-payable passage is close enough. This corpus does not include payroll."
}
```

The nearest file in `corpus/ap/` scores under 0.50, so nothing is returned. The payroll memo is not opened. The gateway log is `search_policy passages=0 on behalf of alice`. Vault is not called. The audit line has `row_count` 0 and `vault_login` false.

The model writes: `The question about staff compensation is outside accounts payable and this corpus does not include payroll.` Then `PASS staff compensation`.

## Bob in engineering

`uid 1001: SPIRE issued spiffe://ap.example/workload/ap-agent aud=pay-gateway`

The program is still the AP agent. The person is not Alice.

`user bob groups engineering`

`exchange http 400` and `caller is not in group accounts-payable`.

The gateway log is `exchange refused: caller is not in group accounts-payable`. There is no MCP call, the model is not asked, and the audit file does not grow. Then `PASS http 400`.

## The notes bot

`uid 1002: SPIRE issued spiffe://ap.example/workload/notes-bot aud=pay-gateway`

SPIRE sees uid 1002 and selects the notes bot. Alice's assertion is presented with that workload token.

`exchange http 400` and `spiffe://ap.example/workload/notes-bot is not on the tool allowlist`.

No tool token, no MCP call, no model, no audit line. Then `PASS http 400`.

## Alice's assertion, unexchanged

The SPIRE line is the AP agent again, and the user line is Alice. There is no `exchange http` line. The agent posts Alice's original assertion straight to `tools/call`.

The response is HTTP 403:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": { "code": -32003, "message": "not an on-behalf-of token" }
}
```

The assertion was signed with the identity-provider key and minted for `pay-gateway`. The tools verify the on-behalf-of key and require audience `ap-payables`. The gateway log is `block: not an on-behalf-of token`. This refusal does write an audit line: `decision` block, `vault_login` false. The model is not asked. Then `PASS http 403`.

## The agent's own JWT at Vault

`uid 1001: SPIRE issued spiffe://ap.example/workload/ap-agent aud=vault.ap.example`

This is the one call that skips the gateway. The audience is `vault.ap.example`. The agent posts that JWT to Vault `auth/jwt/login`, role `pay-gateway`.

Vault answers `invalid subject (sub) claim`. The role is bound to `spiffe://ap.example/workload/pay-gateway`, and this token's subject is the AP agent. Then `PASS vault status 400`.

## After the eight modes

The script prints `demo: all modes passed`.

A payable match leaves no role behind. After `./scripts/call.sh 1001 pay`:

```bash
docker compose exec -T postgres psql -U payables -d payables -c \
  "SELECT count(*) FROM pg_roles WHERE rolname = '$(tr -d '\n' < .state/last_role.txt)'"
```

The count is `0`. The name in `.state/last_role.txt` is the operator's note. The JSON the agent printed does not contain it.
