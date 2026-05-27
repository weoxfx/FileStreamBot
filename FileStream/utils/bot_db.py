"""
Bot SQLite database — stores users, bans, download counters, bot logs.
Separate from the site DB (site_db.py).
"""
import time
import logging
import aiosqlite
from typing import Optional, List
from FileStream.config import BotDB

logger = logging.getLogger(__name__)
DB_PATH = BotDB.PATH


async def init_bot_db():
    import os
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                join_date REAL NOT NULL,
                is_banned INTEGER NOT NULL DEFAULT 0,
                download_count INTEGER NOT NULL DEFAULT 0,
                link_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS file_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_unique_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                mime_type TEXT,
                flog_msg_id INTEGER,
                dump_msg_id INTEGER,
                created_at REAL NOT NULL,
                UNIQUE(user_id, file_unique_id)
            );

            CREATE TABLE IF NOT EXISTS bot_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at REAL NOT NULL
            );
        """)
        await db.commit()
    logger.info("Bot DB initialized at %s", DB_PATH)


async def add_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (id, join_date) VALUES (?, ?)",
            (user_id, time.time())
        )
        await db.commit()


async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def user_exists(user_id: int) -> bool:
    return await get_user(user_id) is not None


async def ban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (id, join_date, is_banned) VALUES (?, ?, 1)",
            (user_id, time.time())
        )
        await db.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (user_id,))
        await db.commit()


async def unban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned = 0 WHERE id = ?", (user_id,))
        await db.commit()


async def is_banned(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user["is_banned"])


async def increment_download(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET download_count = download_count + 1 WHERE id = ?",
            (user_id,)
        )
        await db.commit()


async def get_download_count(user_id: int) -> int:
    user = await get_user(user_id)
    return user["download_count"] if user else 0


async def get_total_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_banned_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_all_users() -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE is_banned = 0") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def log_file(
    user_id: int,
    file_unique_id: str,
    file_name: str,
    file_size: int,
    mime_type: str,
    flog_msg_id: Optional[int] = None,
    dump_msg_id: Optional[int] = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO file_logs
               (user_id, file_unique_id, file_name, file_size, mime_type, flog_msg_id, dump_msg_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, file_unique_id, file_name, file_size, mime_type, flog_msg_id, dump_msg_id, time.time())
        )
        await db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        async with db.execute(
            "SELECT id FROM file_logs WHERE user_id = ? AND file_unique_id = ?",
            (user_id, file_unique_id)
        ) as c:
            row = await c.fetchone()
            return row[0] if row else -1


async def write_bot_log(level: str, message: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bot_logs (level, message, created_at) VALUES (?, ?, ?)",
            (level, message, time.time())
        )
        await db.commit()


async def get_recent_logs(limit: int = 50) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bot_logs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
