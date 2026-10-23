import hashlib
import json
import re
from typing import Any

PROPOSALS_MEDIA_TYPE = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MEDIA_TYPE = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_MEDIA_TYPE = "application/vnd.ga5.invoice-action-receipts+json"

VALID_ACTIONS = {
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def cache_key_for_package(package: dict) -> str:
    return hashlib.sha256(canonical_json(package).encode()).hexdigest()


def extract_refs(text: str) -> list[str]:
    return re.findall(r"\[[^\[\]]{2,200}\]", text)


def package_text(package: Any) -> str:
    return canonical_json(package)


def find_amount_minor(package: dict) -> int:
    direct_keys = ("amountMinor", "amount_minor", "amount")

    for key in direct_keys:
        value = package.get(key)
        if isinstance(value, (int, float)):
            return int(value)

    for value in package.values():
        if isinstance(value, dict):
            for key in direct_keys:
                if isinstance(value.get(key), (int, float)):
                    return int(value[key])

    return 0


def find_field(package: dict, keys: list[str]) -> str:
    for key in keys:
        if package.get(key):
            return str(package[key])

    for value in package.values():
        if isinstance(value, dict):
            for key in keys:
                if value.get(key):
                    return str(value[key])

    return ""


def choose_action(package: dict) -> tuple[str, list[str]]:
    text = package_text(package).lower()
    refs = extract_refs(package_text(package))

    duplicate_signals = [
        "already paid",
        "previously paid",
        "duplicate payment",
        "payment already recorded",
        "same commercial invoice was already paid",
    ]

    conflict_signals = [
        "records conflict",
        "material conflict",
        "conflicting records",
        "material records conflict",
        "amount mismatch",
        "invoice number mismatch",
        "vendor mismatch",
    ]

    hold_signals = [
        "pending verification",
        "awaiting verification",
        "verification pending",
        "awaiting confirmation",
        "pending confirmation",
        "bank confirmation pending",
    ]

    approval_signals = [
        "outside autonomous authority",
        "exceeds autonomous authority",
        "above autonomous authority",
        "outside delegated authority",
        "exceeds delegated authority",
        "requires approval",
        "approval required",
    ]

    def has_any(signals: list[str]) -> bool:
        return any(signal in text for signal in signals)

    if has_any(duplicate_signals):
        return "reject_duplicate", refs

    if has_any(conflict_signals):
        return "open_exception", refs

    if has_any(hold_signals):
        return "hold_invoice", refs

    if has_any(approval_signals):
        return "request_approval", refs

    settlement_is_proven = (
        "reconciled" in text
        and (
            "within autonomous authority" in text
            or "within delegated authority" in text
        )
        and "no prior payment" in text
        and "already paid" not in text
        and "duplicate" not in text
        and "pending" not in text
        and "awaiting" not in text
        and "conflict" not in text
        and "mismatch" not in text
        and "exceeds" not in text
        and "outside" not in text
    )

    if settlement_is_proven:
        return "settle_invoice", refs

    return "hold_invoice", refs


def build_rationale(
    action: str,
    invoice_number: str,
    refs: list[str],
) -> str:
    ref_text = ", ".join(refs[:3]) if refs else "the decisive package evidence"

    explanations = {
        "settle_invoice": (
            "The invoice is reconciled and within autonomous authority, with no "
            "indication of a prior payment for the same commercial invoice."
        ),
        "request_approval": (
            "The invoice appears commercially valid but falls outside delegated "
            "autonomous authority, so approval is required before settlement."
        ),
        "hold_invoice": (
            "Payment must pause until the stated verification is completed."
        ),
        "reject_duplicate": (
            "The same commercial invoice has already been paid, so another "
            "payment would be a duplicate."
        ),
        "open_exception": (
            "Material records conflict, so the invoice requires an exception "
            "workflow rather than automatic settlement."
        ),
    }

    rationale = (
        f"Choose {action} for invoice {invoice_number}. "
        f"The decisive package evidence is {ref_text}. "
        f"{explanations.get(action, '')}"
    )

    return rationale[:1500]


def decide_package(package: dict) -> dict:
    action, refs = choose_action(package)

    evidence_refs = refs[:3]

    while len(evidence_refs) < 3:
        evidence_refs.append("[EVIDENCE-UNRESOLVED]")

    invoice_number = find_field(
        package,
        ["invoiceNumber", "invoice_number", "invoiceNo", "invoiceId"],
    )

    vendor_name = find_field(
        package,
        ["vendorName", "vendor_name", "supplierName", "supplier_name"],
    )

    currency = find_field(package, ["currency", "currencyCode"]) or "INR"

    return {
        "action": action,
        "facts": {
            "vendorName": vendor_name,
            "invoiceNumber": invoice_number,
            "amountMinor": find_amount_minor(package),
            "currency": currency,
        },
        "evidenceRefs": evidence_refs,
        "rationale": build_rationale(action, invoice_number, evidence_refs),
    }


async def ai_decide(package: dict) -> dict:
    """
    Local deterministic decision engine.
    No API key, external model, or network call is required.
    """
    return decide_package(package)
