"""The payment rule, applied to one ledger row.

The prose form of this rule is corpus/ap/ap-payment-policy.md. The numbers come
from the database. A passage cannot flip payable to true.
"""
from __future__ import annotations

from decimal import Decimal


def decide(row: dict) -> dict:
    reasons: list[str] = []
    if row.get("vendor_status") != "approved":
        reasons.append("vendor is not approved")
    if row.get("po_id") is None:
        reasons.append("invoice has no purchase order")
    else:
        amount = Decimal(str(row["amount"]))
        remaining = Decimal(str(row["po_remaining"]))
        if amount > remaining:
            reasons.append("amount exceeds the remaining balance")
    return {
        "invoice_id": row["invoice_id"],
        "vendor": row["vendor"],
        "vendor_status": row["vendor_status"],
        "po_id": row["po_id"],
        "amount": str(row["amount"]),
        "po_remaining": str(row["po_remaining"]),
        "payable": not reasons,
        "reasons": reasons,
    }
