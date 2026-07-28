"""Thin asyncpg pool wrapper. No ORM — migrations (Alembic) own the schema (§5) and
application code talks to it with plain SQL, matching the event-sourced design where
Postgres tables *are* the interface, not an abstraction over them."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool(database_url: str) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool(database_url)
    async with pool.acquire() as conn:
        yield conn
