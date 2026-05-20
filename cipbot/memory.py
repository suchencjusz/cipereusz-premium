from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


@dataclass(slots=True)
class UserMemory:
    user_id: str
    discord_name: str
    general_vibe: str
    dirt: list[str]


class MemoryStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(self.database_path)
        self._connection.row_factory = aiosqlite.Row
        
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA temp_store=MEMORY")
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                discord_name TEXT NOT NULL,
                general_vibe TEXT NOT NULL
            )
            """
        )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_dirt (
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                memory_text TEXT NOT NULL
            )
            """
        )
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def upsert_profile(self, user_id: str, discord_name: str, general_vibe: str) -> None:
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT INTO user_profiles (user_id, discord_name, general_vibe)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                discord_name = excluded.discord_name,
                general_vibe = excluded.general_vibe
            """,
            (user_id, discord_name, general_vibe),
        )

        await self._connection.commit()

    async def add_dirt(self, user_id: str, memory_text: str) -> None:
        assert self._connection is not None

        await self._connection.execute(
            "INSERT INTO user_dirt (user_id, date, memory_text) VALUES (?, ?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat(), memory_text),
        )

        await self._connection.commit()

    async def get_user_memory(self, user_id: str) -> UserMemory | None:
        assert self._connection is not None

        async with self._connection.execute(
            "SELECT user_id, discord_name, general_vibe FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            profile = await cursor.fetchone()

        if profile is None:
            return None

        async with self._connection.execute(
            "SELECT memory_text FROM user_dirt WHERE user_id = ? ORDER BY RANDOM() LIMIT 2",
            (user_id,),
        ) as cursor:
            dirt_rows = await cursor.fetchall()

        return UserMemory(
            user_id=str(profile["user_id"]),
            discord_name=str(profile["discord_name"]),
            general_vibe=str(profile["general_vibe"]),
            dirt=[str(row["memory_text"]) for row in dirt_rows],
        )

    async def get_stats(self) -> dict[str, int]:
        assert self._connection is not None

        async with self._connection.execute("SELECT COUNT(*) AS total FROM user_profiles") as cursor:
            profiles_row = await cursor.fetchone()

        async with self._connection.execute("SELECT COUNT(*) AS total FROM user_dirt") as cursor:
            dirt_row = await cursor.fetchone()

        return {
            "profiles": int(profiles_row["total"]) if profiles_row is not None else 0,
            "dirt": int(dirt_row["total"]) if dirt_row is not None else 0,
        }

    async def get_user_dirt_count(self, user_id: str) -> int:
        assert self._connection is not None

        async with self._connection.execute(
            "SELECT COUNT(*) AS total FROM user_dirt WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        return int(row["total"]) if row is not None else 0
