import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from db import database_lifespan, pool
from decisions import (
    RECEIPTS_MEDIA_TYPE,
    RESULTS_MEDIA_TYPE,
    ai_decide,
    canonical_json,
)

load_dotenv()

A2A_JSON = "application/a2a+json"
BATCH_MEDIA_TYPE = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MEDIA_TYPE = "application/vnd.ga5.invoice-action-proposals+json"

BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with database_lifespan(app):
        yield


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def a2a_content_type_middleware(request: Request, call_next):
    response = await call_next(request)

    if 200 <= response.status_code < 300:
        response.headers["Content-Type"] = A2A_JSON
        response.headers["Cache-Control"] = "no-store"

    return response


@app.exception_handler(StarletteHTTPException)
async def a2a_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    status = exc.status_code

    if status == 404:
        code = "NOT_FOUND"
        message = "Requested A2A resource was not found."
    elif status == 405:
        code = "METHOD_NOT_ALLOWED"
        message = "HTTP method is not allowed for this A2A resource."
    elif status in (401, 403):
        code = "UNAUTHENTICATED"
        message = "Authentication required."
    elif status == 409:
        code = "CONFLICT"
        message = "A2A request conflicts with current task state."
    else:
        code = "A2A_ERROR"
        message = "A2A request failed."

    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        media_type=A2A_JSON,
    )


@app.exception_handler(RequestValidationError)
async def a2a_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Request validation failed.",
            }
        },
        media_type=A2A_JSON,
    )


@app.exception_handler(Exception)
async def a2a_unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unexpected server error.",
            }
        },
        media_type=A2A_JSON,
    )


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        media_type=A2A_JSON,
    )


def get_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")

    if not auth.lower().startswith("bearer "):
        return None

    token = auth[7:].strip()
    return token or None


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_auth(request: Request) -> str:
    token = get_bearer(request)

    if not token:
        raise PermissionError("missing bearer token")

    return owner_hash(token)


def require_a2a_headers(request: Request):
    if request.headers.get("a2a-version") != "1.0":
        raise ValueError("A2A-Version must be 1.0")

    content_type = request.headers.get("content-type", "").split(";")[0].strip()

    if content_type not in {A2A_JSON, "application/json"}:
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


def find_part(parts: list, media_type: str) -> dict | None:
    for part in parts or []:
        if part.get("mediaType") == media_type:
            return part
    return None


def message_fingerprint(message: dict) -> str:
    return hashlib.sha256(canonical_json(message).encode()).hexdigest()


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
                    "tags": [
                        "invoices",
                        "accounts-payable",
                        "safety",
                    ],
                }
            ],
            "securitySchemes": {
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                }
            },
            "security": [
                {"bearer": []}
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

    message_hash = message_fingerprint(message)
    db = await pool()

    async with db.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT task_json, message_hash
            FROM tasks
            WHERE owner_hash = $1 AND message_id = $2
            """,
            owner,
            message_id,
        )

        if existing:
            if existing["message_hash"] != message_hash:
                return error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "messageId was reused with different semantic content.",
                )

            return JSONResponse(
                content={"task": json.loads(existing["task_json"])},
                media_type=A2A_JSON,
            )

        parts = message.get("parts") or []
        batch_part = find_part(parts, BATCH_MEDIA_TYPE)
        result_part = find_part(parts, RESULTS_MEDIA_TYPE)

        if result_part:
            return await handle_result_continuation(
                conn=conn,
                owner=owner,
                message=message,
                message_hash=message_hash,
                result_part=result_part,
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

        if not batch_id or not isinstance(packages, list) or not packages:
            return error(
                400,
                "INVALID_REQUEST",
                "batchId and a non-empty packages array are required.",
            )

        task_id = f"task_{uuid.uuid4().hex}"
        context_id = f"context_{uuid.uuid4().hex}"
        proposals = []

        for package in packages:
            package_id = package.get("packageId")

            if not package_id:
                continue

            decision = await ai_decide(package)
            action_id = uuid.uuid4().hex

            proposals.append(
                {
                    "packageId": package_id,
                    "actionId": action_id,
                    "action": decision["action"],
                    "facts": decision["facts"],
                    "evidenceRefs": decision["evidenceRefs"],
                    "rationale": decision["rationale"],
                }
            )

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
            "executedPackageIds": [],
        }

        task_json = json.dumps(task, separators=(",", ":"))

        try:
            await conn.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    context_id,
                    owner_hash,
                    batch_id,
                    state,
                    task_json,
                    message_id,
                    message_hash
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                """,
                task_id,
                context_id,
                owner,
                batch_id,
                task["state"],
                task_json,
                message_id,
                message_hash,
            )
        except asyncpg.UniqueViolationError:
            existing = await conn.fetchrow(
                """
                SELECT task_json, message_hash
                FROM tasks
                WHERE owner_hash = $1 AND message_id = $2
                """,
                owner,
                message_id,
            )

            if existing and existing["message_hash"] != message_hash:
                return error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "messageId was reused with different semantic content.",
                )

            return JSONResponse(
                content={"task": json.loads(existing["task_json"])},
                media_type=A2A_JSON,
            )

        return JSONResponse(
            content={"task": task_response(task)},
            media_type=A2A_JSON,
        )


async def handle_result_continuation(
    conn,
    owner: str,
    message: dict,
    message_hash: str,
    result_part: dict,
):
    task_id = message.get("taskId")
    context_id = message.get("contextId")
    data = result_part.get("data") or {}
    batch_id = data.get("batchId")
    message_id = message.get("messageId")

    if not task_id:
        return error(400, "INVALID_REQUEST", "taskId is required.")

    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT *
            FROM tasks
            WHERE task_id = $1 AND owner_hash = $2
            FOR UPDATE
            """,
            task_id,
            owner,
        )

        if not row:
            return error(
                403,
                "TASK_FORBIDDEN",
                "Task is not accessible to this principal.",
            )

        task = json.loads(row["task_json"])

        if context_id and context_id != task["contextId"]:
            return error(409, "CONTEXT_MISMATCH", "Context does not match task.")

        if batch_id and batch_id != task["batchId"]:
            return error(409, "BATCH_MISMATCH", "Batch does not match task.")

        if message_id:
            if task.get("lastResultMessageId") == message_id:
                if task.get("lastResultMessageHash") == message_hash:
                    return JSONResponse(
                        content={"task": task_response(task)},
                        media_type=A2A_JSON,
                    )

                return error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "Result messageId was reused with different content.",
                )

        if task["state"] != "TASK_STATE_INPUT_REQUIRED":
            return error(
                409,
                "INVALID_TASK_STATE",
                "Task is not awaiting results.",
            )

        proposal_map = {
            f"{p['packageId']}:{p['actionId']}:{p['action']}": p
            for p in task["proposals"]
        }

        executions = []
        invalid = False

        for result in data.get("results", []):
            package_id = result.get("packageId")
            action_id = result.get("actionId")
            action = result.get("action")
            outcome = result.get("outcome")
            receipt_nonce = result.get("receiptNonce")

            key = f"{package_id}:{action_id}:{action}"
            proposal = proposal_map.get(key)

            if not proposal:
                invalid = True
                break

            if outcome not in {"ACCEPTED", "REJECTED"}:
                invalid = True
                break

            if outcome == "ACCEPTED":
                if not receipt_nonce:
                    invalid = True
                    break

                if package_id in task["executedPackageIds"]:
                    invalid = True
                    break

                executions.append(
                    {
                        "packageId": proposal["packageId"],
                        "actionId": proposal["actionId"],
                        "action": proposal["action"],
                        "receiptNonce": receipt_nonce,
                        "facts": proposal["facts"],
                        "evidenceRefs": proposal["evidenceRefs"],
                    }
                )

                task["executedPackageIds"].append(package_id)

        if invalid:
            return error(
                409,
                "INVALID_CONTINUATION",
                "Result continuation does not match the stored task or proposal.",
            )

        task["history"].append(message)

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
        task["lastResultMessageId"] = message_id
        task["lastResultMessageHash"] = message_hash

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
        content={"task": task_response(task)},
        media_type=A2A_JSON,
    )


@app.get("/a2a/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    try:
        owner = require_auth(request)
        require_a2a_headers(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")
    except ValueError as exc:
        return error(400, "INVALID_REQUEST", str(exc))

    db = await pool()

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM tasks
            WHERE task_id = $1 AND owner_hash = $2
            """,
            task_id,
            owner,
        )

    if not row:
        return error(
            403,
            "TASK_FORBIDDEN",
            "Task is not accessible to this principal.",
        )

    return JSONResponse(
        content=public_task(row),
        media_type=A2A_JSON,
    )


@app.get("/a2a/tasks")
async def list_tasks(request: Request):
    try:
        owner = require_auth(request)
        require_a2a_headers(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")
    except ValueError as exc:
        return error(400, "INVALID_REQUEST", str(exc))

    db = await pool()

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM tasks
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
        require_a2a_headers(request)
    except PermissionError:
        return error(401, "UNAUTHENTICATED", "Authentication required.")
    except ValueError as exc:
        return error(400, "INVALID_REQUEST", str(exc))

    db = await pool()

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT *
                FROM tasks
                WHERE task_id = $1 AND owner_hash = $2
                FOR UPDATE
                """,
                task_id,
                owner,
            )

            if not row:
                return error(
                    403,
                    "TASK_FORBIDDEN",
                    "Task is not accessible to this principal.",
                )

            task = json.loads(row["task_json"])

            if task["state"] in TERMINAL_STATES:
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
