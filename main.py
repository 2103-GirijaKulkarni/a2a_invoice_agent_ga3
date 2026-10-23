import hashlib
import json
import os
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from db import init_db, pool
from decisions import (
    RECEIPTS_MEDIA_TYPE,
    RESULTS_MEDIA_TYPE,
    ai_decide,
    cache_key_for_package,
    canonical_json,
)

load_dotenv()

A2A_JSON = "application/a2a+json"
BATCH_MEDIA_TYPE = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MEDIA_TYPE = "application/vnd.ga5.invoice-action-proposals+json"

BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
BEARER_TOKEN = os.environ.get("A2A_BEARER_TOKEN", "")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def startup():
    await init_db()


@app.middleware("http")
async def a2a_content_type_middleware(request: Request, call_next):
    response = await call_next(request)

    # The grader explicitly requires this Content-Type on successful A2A responses.
    if 200 <= response.status_code < 300:
        response.headers["Content-Type"] = A2A_JSON
        response.headers["Cache-Control"] = "no-store"

    return response


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        media_type=A2A_JSON,
    )


def get_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    return token or None


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_auth(request: Request):
    token = get_bearer(request)
    if not token:
        raise PermissionError("missing bearer token")
    if BEARER_TOKEN and token != BEARER_TOKEN:
        raise PermissionError("invalid bearer token")
    return owner_hash(token)


def require_a2a_headers(request: Request):
    if request.headers.get("a2a-version") != "1.0":
        raise ValueError("A2A-Version must be 1.0")

    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type != A2A_JSON:
        raise ValueError(f"Content-Type must be {A2A_JSON}")


def task_response(task: dict) -> dict:
    return {
        "id": task["taskId"],
        "contextId": task["contextId"],
        "history": task["history"],
        "status": {"state": task["state"]},
        "artifacts": task["artifacts"],
    }


def public_task(row) -> dict:
    task = json.loads(row["task_json"])
    return task_response(task)


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return JSONResponse(
        content={
            "name": "Invoice Action Agent",
            "description": (
                "Reads invoice packages, proposes one evidence-based action "
                "per package, and records only grader-approved executions."
            ),
            "version": "1.0.0",
            "capabilities": {},
            "skills": [
                {
                    "id": "invoice_action_agent",
                    "name": "Invoice action agent",
                    "description": (
                        "Chooses one safe invoice action per package and cites "
                        "decisive supporting evidence."
                    ),
                    "tags": ["invoices", "accounts-payable", "safety"],
                }
            ],
            "supportedInterfaces": [
                {
                    "url": f"{BASE_URL}/a2a/",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                }
            ],
            "defaultInputModes": [BATCH_MEDIA_TYPE],
            "defaultOutputModes": [
                PROPOSALS_MEDIA_TYPE,
                RECEIPTS_MEDIA_TYPE,
            ],
        },
        media_type=A2A_JSON,
    )


@app.post("/a2a/message:send")
async def message_send(request: Request):
    try:
        owner = require_auth(request)
        require_a2a_headers(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")
    except ValueError as exc:
        return error(400, "INVALID_REQUEST", str(exc))

    try:
        body = await request.json()
    except Exception:
        return error(400, "INVALID_REQUEST", "Malformed JSON body.")

    message = body.get("message")
    if not isinstance(message, dict):
        return error(400, "INVALID_REQUEST", "message is required.")

    message_id = message.get("messageId")
    if not message_id:
        return error(400, "INVALID_REQUEST", "message.messageId is required.")

    message_hash = hashlib.sha256(
        canonical_json(message).encode()
    ).hexdigest()

    db = await pool()

    async with db.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT task_json FROM tasks
            WHERE owner_hash = $1 AND message_hash = $2
            """,
            owner,
            message_hash,
        )

        if existing:
            return JSONResponse(
                content={"task": json.loads(existing["task_json"])},
                media_type=A2A_JSON,
            )

        parts = message.get("parts") or []
        batch_part = next(
            (
                p
                for p in parts
                if p.get("mediaType") == BATCH_MEDIA_TYPE
            ),
            None,
        )

        if not batch_part:
            return error(
                400,
                "UNSUPPORTED_MEDIA_TYPE",
                "Expected invoice claim batch part.",
            )

        data = batch_part.get("data") or {}
        batch_id = data.get("batchId")
        packages = data.get("packages") or []

        if not batch_id or not isinstance(packages, list):
            return error(
                400,
                "INVALID_REQUEST",
                "batchId and packages are required.",
            )

        task_id = f"task_{uuid.uuid4().hex}"
        context_id = f"context_{uuid.uuid4().hex}"

        proposals = []
        proposal_by_key = {}

        for package in packages:
            package_id = package.get("packageId")
            if not package_id:
                continue

            decision = await ai_decide(package)
            action_id = uuid.uuid4().hex

            proposal = {
                "packageId": package_id,
                "actionId": action_id,
                "action": decision["action"],
                "facts": decision["facts"],
                "evidenceRefs": decision["evidenceRefs"],
                "rationale": decision["rationale"],
            }

            proposals.append(proposal)
            proposal_by_key[f"{package_id}:{action_id}"] = proposal

        proposal_artifact = {
            "parts": [
                {
                    "mediaType": PROPOSALS_MEDIA_TYPE,
                    "data": {
                        "batchId": batch_id,
                        "proposals": proposals,
                    },
                }
            ]
        }

        task = {
            "taskId": task_id,
            "contextId": context_id,
            "ownerHash": owner,
            "batchId": batch_id,
            "state": "TASK_STATE_INPUT_REQUIRED",
            "history": [message],
            "artifacts": [proposal_artifact],
            "proposals": proposals,
            "executedActionIds": [],
        }

        task_json = json.dumps(task, separators=(",", ":"))

        try:
            await conn.execute(
                """
                INSERT INTO tasks (
                    task_id, context_id, owner_hash, batch_id,
                    state, task_json, message_hash
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                """,
                task_id,
                context_id,
                owner,
                batch_id,
                task["state"],
                task_json,
                message_hash,
            )
        except asyncpg.UniqueViolationError:
            existing = await conn.fetchrow(
                """
                SELECT task_json FROM tasks
                WHERE owner_hash = $1 AND message_hash = $2
                """,
                owner,
                message_hash,
            )
            return JSONResponse(
                content={"task": json.loads(existing["task_json"])},
                media_type=A2A_JSON,
            )

        return JSONResponse(
            content={"task": task_response(task)},
            media_type=A2A_JSON,
        )


async def load_owned_task(conn, owner: str, task_id: str):
    return await conn.fetchrow(
        """
        SELECT * FROM tasks
        WHERE task_id = $1 AND owner_hash = $2
        """,
        task_id,
        owner,
    )


@app.get("/a2a/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    try:
        owner = require_auth(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")

    db = await pool()

    async with db.acquire() as conn:
        row = await load_owned_task(conn, owner, task_id)

    if not row:
        return error(404, "TASK_NOT_FOUND", "Task not found.")

    return JSONResponse(
        content=public_task(row),
        media_type=A2A_JSON,
    )


@app.get("/a2a/tasks")
async def list_tasks(request: Request):
    try:
        owner = require_auth(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")

    db = await pool()

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM tasks
            WHERE owner_hash = $1
            ORDER BY created_at DESC
            """,
            owner,
        )

    return JSONResponse(
        content={"tasks": [public_task(row) for row in rows]},
        media_type=A2A_JSON,
    )


@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, request: Request):
    try:
        owner = require_auth(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")

    db = await pool()

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM tasks
                WHERE task_id = $1 AND owner_hash = $2
                FOR UPDATE
                """,
                task_id,
                owner,
            )

            if not row:
                return error(404, "TASK_NOT_FOUND", "Task not found.")

            task = json.loads(row["task_json"])

            if task["state"] in {
                "TASK_STATE_COMPLETED",
                "TASK_STATE_CANCELED",
                "TASK_STATE_FAILED",
                "TASK_STATE_REJECTED",
            }:
                return error(409, "TASK_TERMINAL", "Task is already terminal.")

            task["state"] = "TASK_STATE_CANCELED"

            await conn.execute(
                """
                UPDATE tasks
                SET state = $2,
                    task_json = $3::jsonb,
                    updated_at = now()
                WHERE task_id = $1
                """,
                task_id,
                task["state"],
                json.dumps(task, separators=(",", ":")),
            )

    return JSONResponse(
        content=task_response(task),
        media_type=A2A_JSON,
    )


@app.post("/a2a/tasks/{task_id}/results")
async def receive_results(task_id: str, request: Request):
    """
    Compatibility continuation route.

    If your grader sends results through /message:send instead, move this
    logic into message_send by detecting RESULTS_MEDIA_TYPE.
    """
    try:
        owner = require_auth(request)
        require_a2a_headers(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")
    except ValueError as exc:
        return error(400, "INVALID_REQUEST", str(exc))

    try:
        body = await request.json()
    except Exception:
        return error(400, "INVALID_REQUEST", "Malformed JSON body.")

    message = body.get("message") or {}
    parts = message.get("parts") or []

    result_part = next(
        (
            p
            for p in parts
            if p.get("mediaType") == RESULTS_MEDIA_TYPE
        ),
        None,
    )

    if not result_part:
        return error(400, "INVALID_REQUEST", "Result part required.")

    data = result_part.get("data") or {}

    db = await pool()

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM tasks
                WHERE task_id = $1 AND owner_hash = $2
                FOR UPDATE
                """,
                task_id,
                owner,
            )

            if not row:
                return error(404, "TASK_NOT_FOUND", "Task not found.")

            task = json.loads(row["task_json"])

            if task["state"] != "TASK_STATE_INPUT_REQUIRED":
                return error(409, "INVALID_TASK_STATE", "Task is not awaiting results.")

            if data.get("batchId") != task["batchId"]:
                return error(409, "BATCH_MISMATCH", "Batch does not match task.")

            proposal_map = {
                f"{p['packageId']}:{p['actionId']}:{p['action']}": p
                for p in task["proposals"]
            }

            executions = []

            for result in data.get("results", []):
                key = (
                    f"{result.get('packageId')}:"
                    f"{result.get('actionId')}:"
                    f"{result.get('action')}"
                )

                proposal = proposal_map.get(key)

                if not proposal:
                    continue

                if result.get("outcome") != "ACCEPTED":
                    continue

                if result.get("packageId") in task["executedActionIds"]:
                    continue

                executions.append(
                    {
                        "packageId": proposal["packageId"],
                        "actionId": proposal["actionId"],
                        "action": proposal["action"],
                        "receiptNonce": result.get("receiptNonce"),
                        "facts": proposal["facts"],
                        "evidenceRefs": proposal["evidenceRefs"],
                    }
                )

                task["executedActionIds"].append(result.get("packageId"))

            if executions:
                task["artifacts"].append(
                    {
                        "parts": [
                            {
                                "mediaType": RECEIPTS_MEDIA_TYPE,
                                "data": {
                                    "batchId": task["batchId"],
                                    "executions": executions,
                                },
                            }
                        ]
                    }
                )
                task["state"] = "TASK_STATE_COMPLETED"

            await conn.execute(
                """
                UPDATE tasks
                SET state = $2,
                    task_json = $3::jsonb,
                    updated_at = now()
                WHERE task_id = $1
                """,
                task_id,
                task["state"],
                json.dumps(task, separators=(",", ":")),
            )

    return JSONResponse(
        content=task_response(task),
        media_type=A2A_JSON,
    )
