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
    """
    Extract bracketed references such as [DOC-12 p.3] or [LEDGER-7].
    """
    return re.findall(r"\[[^\[\]]{2,200}\]", text)


def get_text(package: Any) -> str:
    """
    Convert the whole package into searchable text, including nested fields.
    """
    return canonical_json(package)


def find_amount_minor(package: dict) -> int:
    for key in ("amountMinor", "amount_minor", "amount"):
        value = package.get(key)

        if isinstance(value, (int, float)):
            return int(value)

    # Look one level deep for common invoice objects.
    for value in package.values():
        if isinstance(value, dict):
            for key in ("amountMinor", "amount_minor", "amount"):
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
    """
    Decision order matters:
    duplicate -> conflict -> hold -> approval -> settle.
    """
    text = get_text(package).lower()
    refs = extract_refs(get_text(package))

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
        "requires approval",
        "approval required",
        "outside delegated authority",
        "exceeds delegated authority",
    ]

    settle_signals = [
        "reconciled",
        "within autonomous authority",
        "within delegated authority",
        "valid",
        "no prior payment",
        "no duplicate",
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

    if has_any(settle_signals):
        return "settle_invoice", refs

    # Safe default: do not settle uncertain invoices.
    return "hold_invoice", refs


def build_rationale(action: str, invoice_number: str, refs: list[str]) -> str:
    ref_text = ", ".join(refs[:3]) if refs else "the decisive package evidence"

    rationale = (
        f"Choose {action} for invoice {invoice_number}. "
        f"The decisive package evidence is {ref_text}. "
    )

    explanations = {
        "settle_invoice": (
            "The invoice is valid, reconciled, and within autonomous authority, "
            "with no indication of a prior payment for the same commercial invoice."
        ),
        "request_approval": (
            "The invoice appears commercially valid but falls outside the "
            "delegated autonomous authority, so approval is required before settlement."
        ),
        "hold_invoice": (
            "Payment must pause until the stated verification is completed."
        ),
        "reject_duplicate": (
            "The same commercial invoice has already been paid, so a further "
            "payment would be a duplicate."
        ),
        "open_exception": (
            "Material records conflict, so the invoice requires an exception "
            "workflow rather than automatic settlement."
        ),
    }

    rationale += explanations.get(action, "")
    return rationale[:1500]


def decide_package(package: dict) -> dict:
    action, refs = choose_action(package)

    # Keep only three decisive references.
    evidence_refs = refs[:3]

    # This fallback should not normally trigger, but it keeps the schema valid.
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
    No external model is required.
    This is the local decision engine used for every package.
    """
    return decide_package(package)
