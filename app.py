import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from canonical import sha256_hex, proposal_digest
from schemas import validate_propose, PROFILE
from decide import decide, new_call_id
from store import engine, init, fingerprint_dossier
from receipts import verify_receipt

app = FastAPI()
BASE_URL = "https://a2a-invoice-agent-ga3.onrender.com"

AGENT_CARD = {
    "name": "Safe AI Mailroom Agent",
    "description": (
        "Reads mailroom dossiers, proposes exactly one safe action, "
        "and records only cryptographically verified signed receipts."
    ),
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "url": f"{BASE_URL}/mailroom",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False
    },
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [
        {
            "id": "mailroom_action_gate",
            "name": "Safe mailroom action gate",
            "description": (
                "Classifies a dossier and returns one allowed mailroom "
                "action with minimal evidence, then accepts signed receipts."
            ),
            "tags": ["mailroom", "safety", "receipts", "a2a"],
            "examples": [
                "Propose a safe action for a customer email dossier.",
                "Verify a signed receipt and record its outcome."
            ],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"]
        }
    ],
    "securitySchemes": {},
    "security": []
}


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return JSONResponse(
        status_code=200,
        content=AGENT_CARD,
        media_type="application/json",
    )


@app.get("/.well-known/agent.json")
async def agent_card_alias():
    return JSONResponse(
        status_code=200,
        content=AGENT_CARD,
        media_type="application/json",
    )
init()


def error(message, status_code=400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message},
    )


@app.post("/mailroom")
@app.post("/mailroom/")
async def mailroom(request: Request):
    try:
        body = await request.json()
    except Exception:
        return error("Invalid JSON body", 400)

    if not isinstance(body, dict):
        return error("Request body must be a JSON object", 400)

    operation = body.get("operation")

    if operation == "propose":
        return await handle_propose(body)

    if operation == "commit":
        return await handle_commit(body)

    return error("Unknown operation", 400)


async def handle_propose(body):
    try:
        dossiers = validate_propose(body)
    except ValueError as exc:
        return error(str(exc), 422)

    evaluation_id = body["evaluationId"]
    input_digest = sha256_hex(dossiers)

    with engine.begin() as conn:
        existing = conn.execute(
            text("""
                SELECT input_digest, propose_response
                FROM evaluations
                WHERE evaluation_id = :evaluation_id
            """),
            {"evaluation_id": evaluation_id},
        ).fetchone()

        if existing:
            if existing.input_digest != input_digest:
                return error(
                    "Same evaluationId used with changed dossier content",
                    409,
                )

            return JSONResponse(
                status_code=200,
                content=json.loads(existing.propose_response),
            )

        proposals = []

        for dossier in dossiers:
            fingerprint = fingerprint_dossier(dossier)

            cached = conn.execute(
                text("""
                    SELECT proposal_json, call_id
                    FROM stable_cache
                    WHERE content_fingerprint = :fingerprint
                """),
                {"fingerprint": fingerprint},
            ).fetchone()

            if cached:
                proposal = json.loads(cached.proposal_json)
                call_id = cached.call_id
            else:
                decision = decide(dossier)
                call_id = new_call_id()

                proposal = {
                    "dossierId": dossier["dossierId"],
                    "callId": call_id,
                    "action": decision["action"],
                    "target": decision["target"],
                    "payload": decision["payload"],
                    "evidence": decision["evidence"],
                }

                conn.execute(
                    text("""
                        INSERT INTO stable_cache
                        (content_fingerprint, proposal_json, call_id)
                        VALUES
                        (:fingerprint, :proposal_json, :call_id)
                    """),
                    {
                        "fingerprint": fingerprint,
                        "proposal_json": json.dumps(
                            proposal, separators=(",", ":")
                        ),
                        "call_id": call_id,
                    },
                )

            digest = proposal_digest(proposal)
            proposal_json = json.dumps(proposal, separators=(",", ":"))

            conn.execute(
                text("""
                    INSERT INTO proposals
                    (evaluation_id, dossier_id, content_fingerprint,
                     call_id, proposal_json, proposal_digest)
                    VALUES
                    (:evaluation_id, :dossier_id, :fingerprint,
                     :call_id, :proposal_json, :proposal_digest)
                """),
                {
                    "evaluation_id": evaluation_id,
                    "dossier_id": dossier["dossierId"],
                    "fingerprint": fingerprint,
                    "call_id": call_id,
                    "proposal_json": proposal_json,
                    "proposal_digest": digest,
                },
            )

            proposals.append(proposal)

        response = {
            "profile": PROFILE,
            "evaluationId": evaluation_id,
            "status": "awaiting_receipts",
            "inputDigest": input_digest,
            "proposals": proposals,
        }

        conn.execute(
            text("""
                INSERT INTO evaluations
                (evaluation_id, input_digest, verifier_jwk,
                 propose_response, committed, commit_response)
                VALUES
                (:evaluation_id, :input_digest, :verifier_jwk,
                 :propose_response, FALSE, NULL)
            """),
            {
                "evaluation_id": evaluation_id,
                "input_digest": input_digest,
                "verifier_jwk": json.dumps(
                    body["receiptVerifier"], separators=(",", ":")
                ),
                "propose_response": json.dumps(
                    response, separators=(",", ":")
                ),
            },
        )

        return JSONResponse(status_code=200, content=response)


async def handle_commit(body):
    evaluation_id = body.get("evaluationId")
    input_digest = body.get("inputDigest")
    receipts = body.get("receipts")

    if not isinstance(evaluation_id, str) or not evaluation_id:
        return error("Missing or invalid evaluationId", 400)

    if not isinstance(input_digest, str) or not input_digest:
        return error("Missing or invalid inputDigest", 400)

    if not isinstance(receipts, list) or not receipts:
        return error("Missing or invalid receipts", 400)

    with engine.begin() as conn:
        evaluation = conn.execute(
            text("""
                SELECT input_digest, verifier_jwk, committed, commit_response
                FROM evaluations
                WHERE evaluation_id = :evaluation_id
            """),
            {"evaluation_id": evaluation_id},
        ).fetchone()

        if not evaluation:
            return error("Unknown evaluationId", 400)

        if evaluation.input_digest != input_digest:
            return error("inputDigest does not match stored evaluation", 400)

        if evaluation.committed:
            return JSONResponse(
                status_code=200,
                content=json.loads(evaluation.commit_response),
            )

        stored = {}

        rows = conn.execute(
            text("""
                SELECT call_id, proposal_json, proposal_digest
                FROM proposals
                WHERE evaluation_id = :evaluation_id
            """),
            {"evaluation_id": evaluation_id},
        ).fetchall()

        for row in rows:
            stored[row.call_id] = {
                "proposal": json.loads(row.proposal_json),
                "proposal_digest": row.proposal_digest,
            }

        receipt_call_ids = []

        for receipt in receipts:
            if not isinstance(receipt, dict):
                return error("Each receipt must be an object", 400)

            call_id = receipt.get("callId")

            if not isinstance(call_id, str) or not call_id:
                return error("Receipt missing callId", 400)

            receipt_call_ids.append(call_id)

        if len(receipt_call_ids) != len(set(receipt_call_ids)):
            return error("Duplicate receipt callId", 400)

        if set(receipt_call_ids) != set(stored.keys()):
            return error(
                "Receipts do not exactly match stored proposals", 400
            )

        verifier_jwk = json.loads(evaluation.verifier_jwk)

        for receipt in receipts:
            try:
                verify_receipt(body, receipt, verifier_jwk)
            except Exception:
                return error("Invalid or missing receipt signature", 400)

        outcomes = []

        for receipt in receipts:
            call_id = receipt["callId"]
            item = stored[call_id]
            proposal = item["proposal"]

            if receipt.get("dossierId") != proposal["dossierId"]:
                return error(
                    "Receipt dossierId does not match proposal", 400
                )

            if receipt.get("action") != proposal["action"]:
                return error("Receipt action does not match proposal", 400)

            if receipt.get("proposalDigest") != item["proposal_digest"]:
                return error(
                    "Receipt proposalDigest does not match proposal", 400
                )

            accepted = receipt.get("accepted")

            if not isinstance(accepted, bool):
                return error("Receipt accepted must be boolean", 400)

            conn.execute(
                text("""
                    INSERT INTO receipts
                    (evaluation_id, call_id, receipt_json)
                    VALUES
                    (:evaluation_id, :call_id, :receipt_json)
                    ON CONFLICT (evaluation_id, call_id)
                    DO UPDATE SET receipt_json = EXCLUDED.receipt_json
                """),
                {
                    "evaluation_id": evaluation_id,
                    "call_id": call_id,
                    "receipt_json": json.dumps(
                        receipt, separators=(",", ":")
                    ),
                },
            )

            outcomes.append(
                {
                    "dossierId": receipt["dossierId"],
                    "callId": receipt["callId"],
                    "action": receipt["action"],
                    "proposalDigest": receipt["proposalDigest"],
                    "receiptId": receipt["receiptId"],
                    "status": "executed" if accepted else "rejected",
                }
            )

        response = {
            "profile": PROFILE,
            "evaluationId": evaluation_id,
            "status": "completed",
            "inputDigest": input_digest,
            "outcomes": outcomes,
        }

        conn.execute(
            text("""
                UPDATE evaluations
                SET committed = TRUE,
                    commit_response = :commit_response
                WHERE evaluation_id = :evaluation_id
            """),
            {
                "commit_response": json.dumps(
                    response, separators=(",", ":")
                ),
                "evaluation_id": evaluation_id,
            },
        )

        return JSONResponse(status_code=200, content=response)
