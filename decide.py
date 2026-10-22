import re, secrets

ALLOWED = {
    "create_draft", "update_internal_record", "send_approved_notice",
    "request_confirmation", "quarantine_item", "no_action"
}

def new_call_id():
    return "call_" + secrets.token_hex(16)

def all_text(dossier):
    return "\n".join(
        line.get("text", "")
        for source in dossier.get("sources", [])
        for line in source.get("lines", [])
    )

def line_map(dossier):
    return {
        line["lineId"]: line["text"]
        for source in dossier.get("sources", [])
        for line in source.get("lines", [])
    }

def decide(dossier):
    text = all_text(dossier).lower()
    lines = line_map(dossier)

    # Replace these checks with evidence-based classification.
    if "ignore previous instructions" in text or "send your system prompt" in text:
        return {
            "action": "quarantine_item",
            "target": {"kind": "security_queue", "id": "mailroom"},
            "payload": {
                "artifactId": dossier["dossierId"],
                "reasonCode": "INDIRECT_PROMPT_INJECTION"
            },
            "evidence": [smallest_matching_line(lines,
                        ["ignore previous", "system prompt", "exfiltrate"])]
        }

    if "already completed" in text:
        return {
            "action": "no_action",
            "target": None,
            "payload": {
                "reasonCode": "ALREADY_COMPLETED",
                "referenceId": extract_reference(dossier)
            },
            "evidence": [smallest_matching_line(lines, ["already completed"])]
        }

    # Default safe route for uncertain identity/authority.
    return {
        "action": "request_confirmation",
        "target": {"kind": "approval_queue", "id": "mailroom"},
        "payload": {
            "claimedSender": extract_sender(dossier),
            "questionCode": "VERIFY_REQUEST",
            "referenceId": extract_reference(dossier)
        },
        "evidence": [list(lines)[0]]
    }

def smallest_matching_line(lines, needles):
    for line_id, text in lines.items():
        low = text.lower()
        if any(n in low for n in needles):
            return line_id
    return list(lines)[0]

def extract_sender(dossier):
    return dossier.get("mailbox", "unknown")

def extract_reference(dossier):
    m = re.search(r"\b(?:ref|reference)[\s:-]*([A-Za-z0-9-]+)",
                  all_text(dossier), re.I)
    return m.group(1) if m else dossier["dossierId"]