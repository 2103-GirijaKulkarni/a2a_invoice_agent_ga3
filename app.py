import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from canonical import sha256_hex, proposal_digest
from schemas import validate_propose, PROFILE
from decide import decide, new_call_id
from store import init, db, fingerprint_dossier
from receipts import verify_receipt

app = FastAPI()
init()


def error(message: str, status_code: int = 400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message},
    )


@app.post("/mailroom")
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


async def handle_propose(body: dict):
    try:
        dossiers = validate_propose(body)
    except ValueError as exc:
        return error(str(exc), 422)

    evaluation_id = body["evaluationId"]
    input_digest = sha256_hex(dossiers)

    with db() as conn:
        existing = conn.execute(
            """
            SELECT input_digest, propose_response
            FROM evaluations
            WHERE evaluation_id = ?
            """,
            (evaluation_id,),
        ).fetchone()

        if existing is not None:
            if existing["input_digest"] != input_digest:
                return error(
                    "Same evaluationId used with changed dossier content",
                    409,
                )

            return JSONResponse(
                status_code=200,
                content=json.loads(existing["propose_response"]),
            )

        proposals = []

        for dossier in dossiers:
            content_fingerprint = fingerprint_dossier(dossier)

            cached = conn.execute(
                """
                SELECT proposal_json, call_id
                FROM stable_cache
                WHERE content_fingerprint = ?
                """,
                (content_fingerprint,),
            ).fetchone()

            if cached is not None:
                proposal = json.loads(cached["proposal_json"])
                call_id = cached["call_id"]
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
                    """
                    INSERT OR IGNORE INTO stable_cache
                    (content_fingerprint, proposal_json, call_id)
                    VALUES (?, ?, ?)
                    """,
                    (
                        content_fingerprint,
                        json.dumps(proposal, separators=(",", ":")),
                        call_id,
                    ),
                )

            current_proposal_digest = proposal_digest(proposal)

            conn.execute(
                """
                INSERT INTO proposals
                (evaluation_id, dossier_id, content_fingerprint,
                 call_id, proposal_json, proposal_digest)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    dossier["dossierId"],
                    content_fingerprint,
                    call_id,
                    json.dumps(proposal, separators=(",", ":")),
                    current_proposal_digest,
                ),
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
            """
            INSERT INTO evaluations
            (evaluation_id, input_digest, verifier_jwk,
             propose_response, committed, commit_response)
            VALUES (?, ?, ?, ?, 0, NULL)
            """,
            (
                evaluation_id,
                input_digest,
                json.dumps(body["receiptVerifier"], separators=(",", ":")),
                json.dumps(response, separators=(",", ":")),
            ),
        )

        return JSONResponse(status_code=200, content=response)


async def handle_commit(body: dict):
    evaluation_id = body.get("evaluationId")
    input_digest = body.get("inputDigest")
    receipts = body.get("receipts")

    if not isinstance(evaluation_id, str) or not evaluation_id:
        return error("Missing or invalid evaluationId", 400)

    if not isinstance(input_digest, str) or not input_digest:
        return error("Missing or invalid inputDigest", 400)

    if not isinstance(receipts, list) or not receipts:
        return error("Missing or invalid receipts", 400)

    with db() as conn:
        evaluation = conn.execute(
            """
            SELECT input_digest, verifier_jwk, committed, commit_response
            FROM evaluations
            WHERE evaluation_id = ?
            """,
            (evaluation_id,),
        ).fetchone()

        if evaluation is None:
            return error("Unknown evaluationId", 400)

        if evaluation["input_digest"] != input_digest:
            return error("inputDigest does not match stored evaluation", 400)

        if evaluation["committed"]:
            return JSONResponse(
                status_code=200,
                content=json.loads(evaluation["commit_response"]),
            )

        stored_proposals = {}
        for row in conn.execute(
            """
            SELECT call_id, proposal_json, proposal_digest
            FROM proposals
            WHERE evaluation_id = ?
            """,
            (evaluation_id,),
        ):
            stored_proposals[row["call_id"]] = {
                "proposal": json.loads(row["proposal_json"]),
                "proposal_digest": row["proposal_digest"],
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

        if set(receipt_call_ids) != set(stored_proposals.keys()):
            return error(
                "Receipts do not exactly match stored proposals",
                400,
            )

        verifier_jwk = json.loads(evaluation["verifier_jwk"])

        # Verify every signature before recording any outcome.
        for receipt in receipts:
            try:
                verify_receipt(body, receipt, verifier_jwk)
            except Exception:
                return error("Invalid or missing receipt signature", 400)

        outcomes = []

        for receipt in receipts:
            call_id = receipt["callId"]
            stored = stored_proposals[call_id]
            proposal = stored["proposal"]

            if receipt.get("dossierId") != proposal["dossierId"]:
                return error("Receipt dossierId does not match proposal", 400)

            if receipt.get("action") != proposal["action"]:
                return error("Receipt action does not match proposal", 400)

            if receipt.get("proposalDigest") != stored["proposal_digest"]:
                return error(
                    "Receipt proposalDigest does not match proposal",
                    400,
                )

            accepted = receipt.get("accepted")

            if not isinstance(accepted, bool):
                return error("Receipt accepted must be boolean", 400)

            conn.execute(
                """
                INSERT OR REPLACE INTO receipts
                (evaluation_id, call_id, receipt_json)
                VALUES (?, ?, ?)
                """,
                (
                    evaluation_id,
                    call_id,
                    json.dumps(receipt, separators=(",", ":")),
                ),
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
            """
            UPDATE evaluations
            SET committed = 1,
                commit_response = ?
            WHERE evaluation_id = ?
            """,
            (
                json.dumps(response, separators=(",", ":")),
                evaluation_id,
            ),
        )

        return JSONResponse(status_code=200, content=response)