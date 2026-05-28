"""
Site SQLite database — stores anime series, episodes, and stream tokens.
This is the DB the website queries. Separate from the bot DB.
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
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER NOT NULL REFERENCES anime(id),
                season INTEGER NOT NULL,
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
                season INTEGER NOT NULL,
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
    logger.info("Site DB initialized at %s", DB_PATH)


def _make_token(anime_slug, season, episode, audio_type, quality):
    """
    Generate an HMAC-signed, obfuscated stream token.
    Non-guessable — the site uses this to request streams.
    """
    payload = "{}:{}:{}:{}:{}:{}".format(anime_slug, season, episode, audio_type, quality, int(time.time()))
    sig = hmac.HMAC(
        Site.STREAM_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()[:24]
    rand = base64.urlsafe_b64encode(os.urandom(6)).decode().rstrip("=")
    return "{}{}".format(rand, sig)


async def get_or_create_anime(name, slug):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM anime WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        if row:
            return row[0]
        cur = await db.execute(
            "INSERT INTO anime (name, slug, created_at) VALUES (?, ?, ?)",
            (name, slug, time.time())
        )
        await db.commit()
        return cur.lastrowid


async def upsert_episode(
    anime_id,
    season,
    episode,
    audio_type,
    quality,
    dump_msg_id,
    dump_channel_id,
    file_size,
    anime_slug,
):
    """Insert or replace an episode. Returns the stream token."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT stream_token FROM episodes
               WHERE anime_id = ? AND season = ? AND episode = ? AND audio_type = ? AND quality = ?""",
            (anime_id, season, episode, audio_type, quality)
        ) as cur:
            row = await cur.fetchone()

        if row:
            token = row[0]
            await db.execute(
                """UPDATE episodes SET dump_msg_id=?, dump_channel_id=?, file_size=?
                   WHERE anime_id=? AND season=? AND episode=? AND audio_type=? AND quality=?""",
                (dump_msg_id, dump_channel_id, file_size, anime_id, season, episode, audio_type, quality)
            )
        else:
            token = _make_token(anime_slug, season, episode, audio_type, quality)
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


async def get_episode_by_token(token):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.*, a.name as anime_name, a.slug as anime_slug
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE e.stream_token = ?""",
            (token,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_anime_list():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT a.id, a.name, a.slug,
                      COUNT(e.id) as episode_count,
                      MAX(e.season) as max_season
               FROM anime a LEFT JOIN episodes e ON e.anime_id = a.id
               GROUP BY a.id ORDER BY a.name""",
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_episodes_for_anime(slug, season=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if season is not None:
            async with db.execute(
                """SELECT e.season, e.episode, e.audio_type, e.quality,
                          e.stream_token, e.file_size, e.created_at
                   FROM episodes e JOIN anime a ON e.anime_id = a.id
                   WHERE a.slug = ? AND e.season = ?
                   ORDER BY e.episode, e.audio_type, e.quality""",
                (slug, season)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """SELECT e.season, e.episode, e.audio_type, e.quality,
                          e.stream_token, e.file_size, e.created_at
                   FROM episodes e JOIN anime a ON e.anime_id = a.id
                   WHERE a.slug = ?
                   ORDER BY e.season, e.episode, e.audio_type, e.quality""",
                (slug,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_episode_qualities(slug, season, episode):
    """Return all available quality options for one episode (for the site quality picker)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.audio_type, e.quality, e.stream_token, e.file_size
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE a.slug = ? AND e.season = ? AND e.episode = ?
               ORDER BY e.audio_type, e.quality""",
            (slug, season, episode)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_episode_by_dump_msg(dump_msg_id: int, dump_channel_id: int) -> bool:
    """
    Delete the episode row whose dump message was deleted from Telegram.
    Also removes the parent anime row if it has no episodes left.
    Returns True if a row was deleted.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, anime_id FROM episodes WHERE dump_msg_id = ? AND dump_channel_id = ?",
            (dump_msg_id, dump_channel_id)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return False

        ep_id, anime_id = row
        await db.execute("DELETE FROM episodes WHERE id = ?", (ep_id,))

        # Clean up the anime row if it has no more episodes
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE anime_id = ?", (anime_id,)
        ) as cur:
            count_row = await cur.fetchone()
        if count_row and count_row[0] == 0:
            await db.execute("DELETE FROM anime WHERE id = ?", (anime_id,))

        await db.commit()
        logger.info("Deleted episode for dump_msg_id=%s from channel=%s", dump_msg_id, dump_channel_id)
        return True


async def upsert_subtitle(anime_id, season, episode, label, lang, file_id):
    """Insert or replace a subtitle track for an episode. Returns the subtitle row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id FROM subtitles
               WHERE anime_id = ? AND season = ? AND episode = ? AND lang = ?""",
            (anime_id, season, episode, lang)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                """UPDATE subtitles SET label=?, file_id=?, created_at=?
                   WHERE anime_id=? AND season=? AND episode=? AND lang=?""",
                (label, file_id, time.time(), anime_id, season, episode, lang)
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


async def get_subtitles_for_episode(slug, season, episode):
    """Return all subtitle tracks for a given episode."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.label, s.lang, s.file_id
               FROM subtitles s JOIN anime a ON s.anime_id = a.id
               WHERE a.slug = ? AND s.season = ? AND s.episode = ?
               ORDER BY s.lang""",
            (slug, season, episode)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_subtitle_by_id(sub_id):
    """Return a single subtitle row by its id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM subtitles WHERE id = ?", (sub_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_subtitle_by_id(sub_id):
    """Delete a subtitle track by id. Returns True if deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM subtitles WHERE id = ?", (sub_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM subtitles WHERE id = ?", (sub_id,))
        await db.commit()
        return True


async def delete_episode_by_token(token: str) -> Optional[dict]:
    """
    Delete an episode by stream token.
    Also removes the parent anime row if it has no episodes left.
    Returns the deleted episode dict, or None if not found.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.*, a.name as anime_name, a.slug as anime_slug
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE e.stream_token = ?""",
            (token,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        data = dict(row)
        anime_id = data["anime_id"]

        await db.execute("DELETE FROM episodes WHERE stream_token = ?", (token,))

        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE anime_id = ?", (anime_id,)
        ) as cur:
            count_row = await cur.fetchone()
        if count_row and count_row[0] == 0:
            await db.execute("DELETE FROM anime WHERE id = ?", (anime_id,))

        await db.commit()
        logger.info(
            "Deleted episode token=%s  (%s S%sE%s %s/%s)",
            token, data["anime_name"], data["season"], data["episode"],
            data["audio_type"], data["quality"]
        )
        return data


async def get_episode_by_dump_msg(dump_msg_id: int, dump_channel_id: int):
    """Look up an episode by its dump channel message ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.*, a.name as anime_name, a.slug as anime_slug
               FROM episodes e JOIN anime a ON e.anime_id = a.id
               WHERE e.dump_msg_id = ? AND e.dump_channel_id = ?""",
            (dump_msg_id, dump_channel_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
