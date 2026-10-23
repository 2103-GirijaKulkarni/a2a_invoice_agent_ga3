import os
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
    )

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                owner_hash TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                state TEXT NOT NULL,
                task_json JSONB NOT NULL,
                message_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE UNIQUE INDEX IF NOT EXISTS tasks_owner_message_uq
                ON tasks (owner_hash, message_hash);

            CREATE INDEX IF NOT EXISTS tasks_owner_idx
                ON tasks (owner_hash);

            CREATE TABLE IF NOT EXISTS decision_cache (
                cache_key TEXT PRIMARY KEY,
                decision_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )


async def pool():
    if _pool is None:
        raise RuntimeError("Database not initialized")
    return _pool
