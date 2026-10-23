import os
from contextlib import asynccontextmanager

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]

_pool: asyncpg.Pool | None = None


async def init_db():
    global _pool

    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        ssl="require",
        command_timeout=30,
    )

    async with _pool.acquire() as conn:
        # Create the table for fresh databases.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                owner_hash TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                state TEXT NOT NULL,
                task_json JSONB NOT NULL,
                message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )

        # Migrate an older table that lacks message_id.
        await conn.execute(
            """
            ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS message_id TEXT;
            """
        )

        # Backfill old rows so the unique index can be created.
        await conn.execute(
            """
            UPDATE tasks
            SET message_id = task_id
            WHERE message_id IS NULL;
            """
        )

        # Make message_id non-null for new rows.
        await conn.execute(
            """
            ALTER TABLE tasks
            ALTER COLUMN message_id SET NOT NULL;
            """
        )

        # Remove an incompatible older index, if present.
        await conn.execute(
            """
            DROP INDEX IF EXISTS tasks_owner_message_uq;
            """
        )

        # Create the correct idempotency index.
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS tasks_owner_message_id_uq
                ON tasks (owner_hash, message_id);
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS tasks_owner_idx
                ON tasks (owner_hash);
            """
        )


async def close_db():
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


async def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized.")

    return _pool


@asynccontextmanager
async def database_lifespan(app):
    await init_db()
    try:
        yield
    finally:
        await close_db()
