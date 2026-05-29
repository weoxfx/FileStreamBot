"""
Site SQLite database — stores anime series, episodes, and stream tokens.
This is the DB the website queries. Separate from the bot DB.

Episodes are keyed by anilist_id (not slug/season). Season is stored as 1
internally for all entries; the AniList ID already encodes the season.
"""
import os
import time
import hmac
import hashlib
import base64
import logging
import aiosqlite
from typing import Optional, List
from FileStream.config import SiteDB, Site

logger = logging.getLogger(__name__)
DB_PATH = SiteDB.PATH


async def init_site_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS anime (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anilist_id INTEGER UNIQUE,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER NOT NULL REFERENCES anime(id),
                season INTEGER NOT NULL DEFAULT 1,
                episode INTEGER NOT NULL,
                audio_type TEXT NOT NULL,
                quality TEXT NOT NULL,
                stream_token TEXT NOT NULL UNIQUE,
                dump_msg_id INTEGER,
                dump_channel_id INTEGER,
                file_size INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE(anime_id, season, episode, audio_type, quality)
            );

            CREATE TABLE IF NOT EXISTS subtitles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER NOT NULL REFERENCES anime(id),
                season INTEGER NOT NULL DEFAULT 1,
                episode INTEGER NOT NULL,
                label TEXT NOT NULL DEFAULT 'Subtitle',
                lang TEXT NOT NULL DEFAULT 'en',
                file_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(anime_id, season, episode, lang)
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_anime
                ON episodes(anime_id, season, episode);
            CREATE INDEX IF NOT EXISTS idx_episodes_token
                ON episodes(stream_token);
            CREATE INDEX IF NOT EXISTS idx_subtitles_episode
                ON subtitles(anime_id, season, episode);
        """)
        await db.commit()

    # Migration: add anilist_id column to anime table if it doesn't exist yet
    async with aiosqlite.connect(DB_PATH) as db:
        for col, typedef in [
            ("anilist_id",     "INTEGER"),
            ("mal_id",         "INTEGER"),
            ("cover_url",      "TEXT DEFAULT ''"),
            ("synopsis",       "TEXT DEFAULT ''"),
            ("total_episodes", "INTEGER"),
        ]:
            try:
                await db.execute(f"ALTER TABLE anime ADD COLUMN {col} {typedef}")
                await db.commit()
                logger.info("Migrated anime table: added column %s", col)
            except Exception:
                pass  # Column already exists

    logger.info("Site DB initialized at %s", DB_PATH)


def _make_token(anilist_id, episode, audio_type, quality):
    payload = "{}:{}:{}:{}:{}".format(anilist_id, episode, audio_type, quality, int(time.time()))
    sig = hmac.HMAC(
        Site.STREAM_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:24]
    rand = base64.urlsafe_b64encode(os.urandom(6)).decode().rstrip("=")
    return "{}{}".format(rand, sig)


# ── Anime ────────────────────────────────────────────────────────────────────

async def get_or_create_anime(
    name: str,
    slug: str,
    anilist_id: Optional[int] = None,
    mal_id: Optional[int] = None,
    cover_url: str = "",
    synopsis: str = "",
    total_episodes: Optional[int] = None,
) -> int:
    """Return internal anime.id, creating/updating the row as needed."""
    async with aiosqlite.connect(DB_PATH) as db:
        if anilist_id:
            async with db.execute("SELECT id FROM anime WHERE anilist_id = ?", (anilist_id,)) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    """UPDATE anime SET name=?, slug=?, mal_id=?, cover_url=?,
                       synopsis=?, total_episodes=? WHERE anilist_id=?""",
                    (name, slug, mal_id, cover_url, synopsis, total_episodes, anilist_id)
                )
                await db.commit()
                return row[0]

        async with db.execute("SELECT id FROM anime WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                """UPDATE anime SET anilist_id=?, name=?, mal_id=?, cover_url=?,
                   synopsis=?, total_episodes=? WHERE slug=?""",
                (anilist_id, name, mal_id, cover_url, synopsis, total_episodes, slug)
            )
            await db.commit()
            return row[0]

        cur = await db.execute(
            """INSERT INTO anime (anilist_id, name, slug, mal_id, cover_url,
               synopsis, total_episodes, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (anilist_id, name, slug, mal_id, cover_url, synopsis, total_episodes, time.time())
        )
        await db.commit()
        return cur.lastrowid


async def get_anime_by_anilist_id(anilist_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM anime WHERE anilist_id = ?", (anilist_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Episodes ─────────────────────────────────────────────────────────────────

async def upsert_episode(
    anime_id: int,
    episode: int,
    audio_type: str,
    quality: str,
    dump_msg_id: int,
    dump_channel_id: int,
    file_size: int,
    anilist_id: int,
    season: int = 1,
) -> str:
    """Insert or update an episode. Returns the stream token."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT stream_token FROM episodes
               WHERE anime_id=? AND season=? AND episode=? AND audio_type=? AND quality=?""",
            (anime_id, season, episode, audio_type, quality)
        ) as cur:
            row = await cur.fetchone()

        if row:
            token = row[0]
            await db.execute(
                """UPDATE episodes SET dump_msg_id=?, dump_channel_id=?, file_size=?
                   WHERE anime_id=? AND season=? AND episode=? AND audio_type=? AND quality=?""",
                (dump_msg_id, dump_channel_id, file_size,
                 anime_id, season, episode, audio_type, quality)
            )
        else:
            token = _make_token(anilist_id, episode, audio_type, quality)
            await db.execute(
                """INSERT INTO episodes
                   (anime_id, season, episode, audio_type, quality, stream_token,
                    dump_msg_id, dump_channel_id, file_size, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (anime_id, season, episode, audio_type, quality, token,
                 dump_msg_id, dump_channel_id, file_size, time.time())
            )
        await db.commit()
        return token


async def get_episode_by_token(token: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.*, a.name AS anime_name, a.slug AS anime_slug, a.anilist_id
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE e.stream_token = ?""",
            (token,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_episode_by_dump_msg(dump_msg_id: int, dump_channel_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.*, a.name AS anime_name, a.slug AS anime_slug, a.anilist_id
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE e.dump_msg_id=? AND e.dump_channel_id=?""",
            (dump_msg_id, dump_channel_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_anime_list() -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT a.id, a.anilist_id, a.name, a.slug,
                      COUNT(DISTINCT e.episode) AS episode_count
               FROM anime a LEFT JOIN episodes e ON e.anime_id = a.id
               GROUP BY a.id ORDER BY a.name"""
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_episodes_for_anime(anilist_id: int, episode: Optional[int] = None) -> List[dict]:
    """Return episodes for an anime, optionally filtered to a single episode number."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if episode is not None:
            async with db.execute(
                """SELECT e.episode, e.audio_type, e.quality,
                          e.stream_token, e.file_size, e.created_at
                   FROM episodes e JOIN anime a ON e.anime_id = a.id
                   WHERE a.anilist_id=? AND e.episode=?
                   ORDER BY e.audio_type, e.quality""",
                (anilist_id, episode)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """SELECT e.episode, e.audio_type, e.quality,
                          e.stream_token, e.file_size, e.created_at
                   FROM episodes e JOIN anime a ON e.anime_id = a.id
                   WHERE a.anilist_id=?
                   ORDER BY e.episode, e.audio_type, e.quality""",
                (anilist_id,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_episode_qualities(anilist_id: int, episode: int) -> List[dict]:
    """All quality options for a specific episode."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.audio_type, e.quality, e.stream_token, e.file_size,
                      e.dump_msg_id, e.dump_channel_id
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE a.anilist_id=? AND e.episode=?
               ORDER BY e.audio_type, e.quality""",
            (anilist_id, episode)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_episode_qualities_by_slug(slug: str, season: int, episode: int) -> List[dict]:
    """Legacy: look up by slug+season+episode (used by player route)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.audio_type, e.quality, e.stream_token, e.file_size,
                      e.dump_msg_id, e.dump_channel_id
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE a.slug=? AND e.season=? AND e.episode=?
               ORDER BY e.audio_type, e.quality""",
            (slug, season, episode)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_episode_by_token(token: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.*, a.name AS anime_name, a.slug AS anime_slug, a.anilist_id
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE e.stream_token=?""",
            (token,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        anime_id = data["anime_id"]
        await db.execute("DELETE FROM episodes WHERE stream_token=?", (token,))
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE anime_id=?", (anime_id,)
        ) as cur:
            cnt = await cur.fetchone()
        if cnt and cnt[0] == 0:
            await db.execute("DELETE FROM anime WHERE id=?", (anime_id,))
        await db.commit()
        logger.info("Deleted episode token=%s (%s E%s)", token, data["anime_name"], data["episode"])
        return data


async def delete_episode_by_dump_msg(dump_msg_id: int, dump_channel_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, anime_id FROM episodes WHERE dump_msg_id=? AND dump_channel_id=?",
            (dump_msg_id, dump_channel_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        ep_id, anime_id = row
        await db.execute("DELETE FROM episodes WHERE id=?", (ep_id,))
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE anime_id=?", (anime_id,)
        ) as cur:
            cnt = await cur.fetchone()
        if cnt and cnt[0] == 0:
            await db.execute("DELETE FROM anime WHERE id=?", (anime_id,))
        await db.commit()
        return True


# ── Subtitles ────────────────────────────────────────────────────────────────

async def upsert_subtitle(
    anime_id: int, episode: int, label: str, lang: str, file_id: str, season: int = 1
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM subtitles WHERE anime_id=? AND season=? AND episode=? AND lang=?",
            (anime_id, season, episode, lang)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE subtitles SET label=?, file_id=?, created_at=? WHERE id=?",
                (label, file_id, time.time(), row[0])
            )
            sub_id = row[0]
        else:
            cur = await db.execute(
                """INSERT INTO subtitles (anime_id, season, episode, label, lang, file_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (anime_id, season, episode, label, lang, file_id, time.time())
            )
            sub_id = cur.lastrowid
        await db.commit()
        return sub_id


async def get_subtitles_for_episode(anilist_id: int, episode: int) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.label, s.lang, s.file_id
               FROM subtitles s JOIN anime a ON s.anime_id = a.id
               WHERE a.anilist_id=? AND s.episode=?
               ORDER BY s.lang""",
            (anilist_id, episode)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_subtitles_for_episode_by_slug(slug: str, season: int, episode: int) -> List[dict]:
    """Legacy slug-based lookup used by the player."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.label, s.lang, s.file_id
               FROM subtitles s JOIN anime a ON s.anime_id = a.id
               WHERE a.slug=? AND s.season=? AND s.episode=?
               ORDER BY s.lang""",
            (slug, season, episode)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_subtitle_by_id(sub_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM subtitles WHERE id=?", (sub_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_subtitle_by_id(sub_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM subtitles WHERE id=?", (sub_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM subtitles WHERE id=?", (sub_id,))
        await db.commit()
        return True
