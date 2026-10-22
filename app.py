import uuid
from datetime import datetime, timezone


def extract_a2a_text(params):
    message = params.get("message", {})
    parts = message.get("parts", [])

    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            texts.append(part.get("text", ""))

    return "\n".join(texts).strip()


@app.post("/mailroom/message:send")
async def a2a_message_send(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": "Parse error"
                }
            },
            media_type="application/json",
        )

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method not in ("message/send", "SendMessage"):
        return JSONResponse(
            status_code=200,
            content={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32601,
                    "message": "Method not found"
                }
            },
            media_type="application/json",
        )

    incoming_message = params.get("message") or {}
    context_id = incoming_message.get("contextId") or str(uuid.uuid4())
    user_text = extract_a2a_text(params)

    reply_text = (
        "Safe AI Mailroom Agent is ready. "
        "Send a mailroom dossier through the mailroom propose endpoint; "
        "I will return one safe action and wait for a signed receipt."
    )

    if user_text:
        reply_text = (
            "Received your message. This A2A interface provides agent "
            "discovery and status; mailroom dossiers are processed through "
            "the mailroom propose and commit operations."
        )

    now = datetime.now(timezone.utc).isoformat()

    result = {
        "kind": "task",
        "id": str(uuid.uuid4()),
        "contextId": context_id,
        "status": {
            "state": "completed",
            "timestamp": now,
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "agent",
                "parts": [
                    {
                        "kind": "text",
                        "text": reply_text
                    }
                ]
            }
        },
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "mailroom-agent-status",
                "parts": [
                    {
                        "kind": "text",
                        "text": reply_text
                    }
                ]
            }
        ]
    }

    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": result
        },
        media_type="application/json",
    )
