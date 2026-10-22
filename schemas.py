ALLOWED_ACTIONS = {
    "create_draft", "update_internal_record", "send_approved_notice",
    "request_confirmation", "quarantine_item", "no_action"
}

PROFILE = "ga5-mailroom-action-gate/v2"

def require(cond, msg):
    if not cond:
        raise ValueError(msg)

def validate_propose(body: dict):
    require(body.get("profile") == PROFILE, "bad profile")
    require(body.get("operation") == "propose", "wrong operation")
    require(isinstance(body.get("evaluationId"), str), "bad evaluationId")
    require("receiptVerifier" in body, "missing receiptVerifier")
    require(body["receiptVerifier"].get("algorithm") == "Ed25519", "bad algorithm")

    dossiers = body.get("dossiers")
    require(isinstance(dossiers, list) and dossiers, "dossiers required")

    ids = [d.get("dossierId") for d in dossiers]
    require(all(isinstance(i, str) and i for i in ids), "bad dossierId")
    require(len(ids) == len(set(ids)), "duplicate dossierId")

    for d in dossiers:
        line_ids = [
            line.get("lineId")
            for source in d.get("sources", [])
            for line in source.get("lines", [])
        ]
        require(all(isinstance(x, str) and x for x in line_ids), "bad lineId")
        require(len(line_ids) == len(set(line_ids)), "duplicate lineId")

    return dossiers