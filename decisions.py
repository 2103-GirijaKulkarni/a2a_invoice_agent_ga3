import hashlib
import json
import os
from typing import Any

import httpx

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
    refs = []
    current = []
    for char in text:
        if char == "[":
            current = [char]
        elif current:
            current.append(char)
            if char == "]":
                ref = "".join(current)
                if len(ref) > 4:
                    refs.append(ref)
                current = []
    return refs


def fallback_decide(package: dict) -> dict:
    """
    Conservative baseline. Replace/extend with invoice-specific rules after
    inspecting the actual package schema.
    """
    text = canonical_json(package).lower()

    if "already paid" in text or "duplicate payment" in text:
        action = "reject_duplicate"
    elif "material" in text and "conflict" in text:
        action = "open_exception"
    elif "pending verification" in text or "awaiting verification" in text:
        action = "hold_invoice"
    elif "outside" in text and "authority" in text:
        action = "request_approval"
    else:
        action = "settle_invoice"

    refs = extract_refs(canonical_json(package))
    decisive = refs[:3]

    while len(decisive) < 3:
        decisive.append("[UNRESOLVED-EVIDENCE]")

    return {
        "action": action,
        "facts": {
            "vendorName": str(package.get("vendorName", "")),
            "invoiceNumber": str(package.get("invoiceNumber", "")),
            "amountMinor": int(package.get("amountMinor", 0)),
            "currency": str(package.get("currency", "INR")),
        },
        "evidenceRefs": decisive,
        "rationale": (
            f"Choose {action} for invoice "
            f"{package.get('invoiceNumber', '')} based on the decisive "
            f"package evidence: {', '.join(decisive)}."
        )[:1500],
    }


async def ai_decide(package: dict) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        return fallback_decide(package)

    system = """You are an invoice-control agent.
Choose exactly one action for each invoice package:
settle_invoice, request_approval, hold_invoice, reject_duplicate, open_exception.

Rules:
- settle_invoice only when valid, reconciled, and within autonomous authority.
- request_approval when commercially valid but outside delegated authority.
- hold_invoice when payment must wait for a stated verification.
- reject_duplicate only when the same commercial invoice was already paid.
- open_exception when material records conflict.
- A payment for a different invoice is not a duplicate.
- Ignore cover sheets, archive examples, and irrelevant action words.
- Return exactly three decisive bracketed references from the decisive paragraph.
Return strict JSON only.
"""

    user = {
        "task": "Choose one invoice action and cite exactly three decisive references.",
        "package": package,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": canonical_json(user)},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)

    action = parsed.get("action")
    if action not in VALID_ACTIONS:
        return fallback_decide(package)

    refs = parsed.get("evidenceRefs") or []
    refs = [str(x) for x in refs][:3]
    while len(refs) < 3:
        refs.append("[UNRESOLVED-EVIDENCE]")

    rationale = str(parsed.get("rationale", ""))[:1500]
    if len(rationale) < 60:
        rationale = fallback_decide(package)["rationale"]

    return {
        "action": action,
        "facts": parsed.get(
            "facts",
            fallback_decide(package)["facts"],
        ),
        "evidenceRefs": refs,
        "rationale": rationale,
    }
