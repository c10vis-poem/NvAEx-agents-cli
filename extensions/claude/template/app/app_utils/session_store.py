# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Keep Claude session transcripts in Cloud SQL, so any instance can resume them.

Adapted from the Claude Agent SDK's reference Postgres adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import asyncpg
from claude_agent_sdk import SessionKey, SessionListSubkeysKey, SessionStoreEntry

# The table name is interpolated into SQL (identifiers can't be parameters).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Per instance; Cloud SQL's connection limit is shared by all instances.
POOL_SIZE = 5


class PostgresSessionStore:
    """A `SessionStore` over a Postgres table, created on first use.

    One row per transcript entry; the main transcript has `subpath = ''`.
    """

    def __init__(
        self,
        table: str = "claude_session_store",
        **connect: Any,
    ) -> None:
        """`connect` is passed to `asyncpg.create_pool` (host, user, ...)."""
        if not _IDENT_RE.match(table):
            raise ValueError(f"table {table!r} must match [A-Za-z_][A-Za-z0-9_]*")
        self._table = table
        self._connect = connect
        self._pool: asyncpg.Pool | None = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def from_env(cls) -> PostgresSessionStore | None:
        """A store for the Cloud SQL instance in the environment, or None."""
        instance = os.environ.get("INSTANCE_CONNECTION_NAME")
        password = os.environ.get("DB_PASS")
        user = os.environ.get("DB_USER")
        database = os.environ.get("DB_NAME", "postgres")
        if not instance or not password:
            return None
        return cls(
            host=f"/cloudsql/{instance}",
            user=user or "postgres",
            password=password,
            database=database,
        )

    async def _get_pool(self) -> asyncpg.Pool:
        # A pool belongs to the event loop that created it, so a new loop
        # (e.g. `anyio.run` per turn) gets a new pool.
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._pool_loop is loop:
            return self._pool
        if self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        async with self._lock:
            if self._pool is not None and self._pool_loop is loop:
                return self._pool
            if self._pool is not None:
                # Its loop is gone, so it can't be closed gracefully.
                self._pool.terminate()
            pool = await asyncpg.create_pool(
                min_size=1, max_size=POOL_SIZE, **self._connect
            )
            async with pool.acquire() as conn, conn.transaction():
                # Serializes instances that start at once: concurrent
                # CREATE TABLE IF NOT EXISTS can still collide in Postgres.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", self._table
                )
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                      project_key text   NOT NULL,
                      session_id  text   NOT NULL,
                      subpath     text   NOT NULL DEFAULT '',
                      seq         bigserial,
                      entry       jsonb  NOT NULL,
                      mtime       bigint NOT NULL,
                      PRIMARY KEY (project_key, session_id, subpath, seq)
                    )
                    """
                )
            self._pool, self._pool_loop = pool, loop
            return pool

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        pool = await self._get_pool()
        # One multi-row INSERT per batch: atomic, and in batch order.
        await pool.execute(
            f"""
            INSERT INTO {self._table} (project_key, session_id, subpath, entry, mtime)
            SELECT $1, $2, $3, e,
                   (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
            FROM unnest($4::jsonb[]) WITH ORDINALITY AS t(e, ord)
            ORDER BY ord
            """,
            key["project_key"],
            key["session_id"],
            key.get("subpath") or "",
            [json.dumps(e) for e in entries],
        )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        pool = await self._get_pool()
        rows = await pool.fetch(
            f"""
            SELECT entry FROM {self._table}
            WHERE project_key = $1 AND session_id = $2 AND subpath = $3
            ORDER BY seq
            """,
            key["project_key"],
            key["session_id"],
            key.get("subpath") or "",
        )
        # jsonb reorders object keys; the SessionStore contract allows that.
        return [json.loads(row["entry"]) for row in rows] or None

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """Subagent transcripts of a session, so they are restored on resume."""
        pool = await self._get_pool()
        rows = await pool.fetch(
            f"""
            SELECT DISTINCT subpath FROM {self._table}
            WHERE project_key = $1 AND session_id = $2 AND subpath <> ''
            """,
            key["project_key"],
            key["session_id"],
        )
        return [row["subpath"] for row in rows]

    async def exists(self, key: SessionKey) -> bool:
        """Whether the key has a transcript, without loading it."""
        pool = await self._get_pool()
        return await pool.fetchval(
            f"""
            SELECT EXISTS (
              SELECT 1 FROM {self._table}
              WHERE project_key = $1 AND session_id = $2 AND subpath = $3
            )
            """,
            key["project_key"],
            key["session_id"],
            key.get("subpath") or "",
        )
