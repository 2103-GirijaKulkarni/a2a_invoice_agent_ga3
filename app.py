import json
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from canonical import sha256_hex, proposal_digest
from schemas import validate_propose, PROFILE
from decide import decide, new_call_id
from store import engine, init, fingerprint_dossier
from receipts import verify_receipt

app = FastAPI()
init()

BASE_URL = "https://a2a-invoice-agent-ga3.onrender.com"

AGENT_CARD = {
    "name": "Safe AI Mailroom Agent",
    "description": (
        "Reads mailroom dossiers, proposes exactly one safe action, "
        "and records only cryptographically verified signed receipts."
    ),
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "preferredTransport": "JSONRPC",
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

def error(message, status_code=400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message},
    )


def jsonrpc_error(rpc_id, code, message):
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": code,
                "message": message
            }
        },
        media_type="application/json",
    )


def extract_a2a_text(params):
    message = params.get("message") or {}
    parts = message.get("parts") or []

    texts = []

    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            texts.append(part.get("text", ""))

    return "\n".join(texts).strip()

@app.post("/mailroom/message:send")
async def a2a_message_send(request: Request):
    ...

@app.post("/mailroom")
@app.post("/mailroom/")
async def mailroom(request: Request):
    ...
