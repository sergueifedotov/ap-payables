#!/usr/bin/env python3
"""Check retrieval, the payment rule, and the exchange without SPIRE or Vault.

    python3 -m gateway.check_rules
"""
from __future__ import annotations

from pathlib import Path

from gateway.pay import decide
from gateway.retrieve import load_chunks, rank, search
from gateway.tokens import (
    actor_matches,
    authorize_exchange,
    issue_obo,
    mint_user_assertion,
    read_subject_token,
    verify_obo,
)

ROOT = Path(__file__).resolve().parents[1]
AP = ROOT / "corpus" / "ap"
IDP = "ap-idp-hmac-key-not-for-production-use"
OBO = "ap-obo-hmac-key-not-for-production-use"
GATEWAY_AUD = "pay-gateway"
RESOURCE_AUD = "ap-payables"
AGENT = "spiffe://ap.example/workload/ap-agent"
NOTES = "spiffe://ap.example/workload/notes-bot"
GROUPS = {"accounts-payable"}
CLAUSE = "does not exceed the purchase order's remaining balance"


def main() -> None:
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        if not ok:
            failures.append(name)

    chunks = load_chunks(AP)
    texts = {chunk["source"]: chunk["text"] for chunk in chunks}
    check("the clause is in the payment policy", CLAUSE in texts.get("ap-payment-policy.md", ""))
    check("payroll is not in the AP corpus", all("compensation figures" not in chunk["text"] for chunk in chunks))
    ranked = rank(
        [
            {"source": "ap-payment-policy.md", "text": "pay", "vector": [1.0, 0.0]},
            {"source": "vendor-onboarding.md", "text": "w-9", "vector": [0.0, 1.0]},
        ],
        [1.0, 0.0],
        2,
    )
    check("cosine rank puts the nearest passage first", bool(ranked) and ranked[0]["source"] == "ap-payment-policy.md")
    below = rank([{"source": "ap-payment-policy.md", "text": "pay", "vector": [1.0, 0.0]}], [0.4, 0.9165], 2)
    check("a weak score returns no passage", below == [])
    try:
        passages = search(AP, "When can we pay a vendor invoice?")
    except Exception as exc:  # noqa: BLE001
        passages = []
        check(f"embedding search reached Ollama ({exc})", False)
    else:
        check("embedding search returns the payment policy first", bool(passages) and passages[0]["source"] == "ap-payment-policy.md")
        staff = search(AP, "What is staff compensation?")
        check("staff compensation is below the floor", staff == [])

    ok_row = decide({
        "invoice_id": 4411, "vendor": "northwind", "vendor_status": "approved",
        "po_id": 900, "amount": "9800", "po_remaining": "10000",
    })
    check("4411 is payable", ok_row["payable"] is True and ok_row["reasons"] == [])
    over = decide({
        "invoice_id": 4412, "vendor": "northwind", "vendor_status": "approved",
        "po_id": 900, "amount": "12000", "po_remaining": "10000",
    })
    check("4412 exceeds the balance", over["payable"] is False and "amount exceeds the remaining balance" in over["reasons"])
    blocked = decide({
        "invoice_id": 4413, "vendor": "contoso", "vendor_status": "blocked",
        "po_id": 901, "amount": "1000", "po_remaining": "5000",
    })
    check("4413 vendor is blocked", blocked["payable"] is False and "vendor is not approved" in blocked["reasons"])

    alice, err = read_subject_token(
        mint_user_assertion("alice", ["accounts-payable"], GATEWAY_AUD, IDP), IDP, OBO, GATEWAY_AUD,
    )
    check("alice can be read", err is None and alice is not None)
    allowed = authorize_exchange(
        alice or {}, AGENT, RESOURCE_AUD,
        allowed_groups=GROUPS, allowed_actors={AGENT}, resource_audience=RESOURCE_AUD,
        group_name="accounts-payable",
    )
    check("alice and the ap agent may exchange", allowed is None)
    bob, _ = read_subject_token(
        mint_user_assertion("bob", ["engineering"], GATEWAY_AUD, IDP), IDP, OBO, GATEWAY_AUD,
    )
    refused = authorize_exchange(
        bob or {}, AGENT, RESOURCE_AUD,
        allowed_groups=GROUPS, allowed_actors={AGENT}, resource_audience=RESOURCE_AUD,
        group_name="accounts-payable",
    )
    check("engineering is refused", refused == "caller is not in group accounts-payable")
    notes = authorize_exchange(
        alice or {}, NOTES, RESOURCE_AUD,
        allowed_groups=GROUPS, allowed_actors={AGENT}, resource_audience=RESOURCE_AUD,
        group_name="accounts-payable",
    )
    check("notes bot is refused", notes is not None and "allowlist" in notes)

    token = issue_obo("alice", AGENT, ["accounts-payable"], RESOURCE_AUD, OBO)
    obo, obo_err = verify_obo(token, OBO, RESOURCE_AUD, "accounts-payable")
    check("tool token names alice and the agent", obo_err is None and obo is not None and obo["sub"] == "alice" and obo["act"]["sub"] == AGENT)
    _, again = read_subject_token(token, IDP, OBO, GATEWAY_AUD)
    check("second exchange is refused", again == "token has already been exchanged")
    user = mint_user_assertion("alice", ["accounts-payable"], GATEWAY_AUD, IDP)
    _, passed = verify_obo(user, OBO, RESOURCE_AUD, "accounts-payable")
    check("assertion is not a tool token", passed == "not an on-behalf-of token")
    check("a different workload does not match", actor_matches(obo or {}, NOTES) == "actor does not match the workload")

    if failures:
        print("FAIL")
        for item in failures:
            print(f"  {item}")
        raise SystemExit(1)
    print("PASS payables checks" if not failures else "FAIL")


if __name__ == "__main__":
    main()
