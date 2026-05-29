"""
Subtitle upload plugin.

Send a .vtt or .srt file with a caption in this format:
    AniList ID | Episode | Language Label | lang_code

Example:
    21355 | 1 | English | en
    21355 | 1 | Arabic  | ar

The bot stores the file_id and links it to the episode in the DB.
Subtitles are then served at /subtitle/<id> and auto-loaded by the player.
"""
import logging

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.utils import site_db
from FileStream.utils.anilist import fetch_anime_by_id

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = (".vtt", ".srt", ".ass", ".ssa")


def _parse_sub_caption(text: str):
    """
    Parse:  AniList ID | Episode | Language Label | lang_code
    Returns dict or None.
    """
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 4:
        return None
    anilist_s, episode_s, label, lang = parts
    try:
        anilist_id = int(anilist_s)
        episode    = int(episode_s)
    except ValueError:
        return None
    if not lang:
        return None
    return {
        "anilist_id": anilist_id,
        "episode":    episode,
        "label":      label or "Subtitle",
        "lang":       lang.lower(),
    }


def _is_subtitle(message: Message) -> bool:
    if message.document and message.document.file_name:
        return message.document.file_name.lower().endswith(SUPPORTED_EXTS)
    return False


@FileStream.on_message(
    filters.private
    & filters.document
    & filters.user([Telegram.OWNER_ID] + list(Telegram.AUTH_USERS)),
    group=2,
)
async def subtitle_file_handler(bot: Client, message: Message):
    if not _is_subtitle(message):
        return

    caption_raw = (message.caption or "").strip()
    if not caption_raw or "|" not in caption_raw:
        await message.reply_text(
            "❌ <b>Missing subtitle caption.</b>\n\n"
            "Format:\n<code>AniList ID | Episode | Language Label | lang_code</code>\n\n"
            "Example:\n<code>21355 | 1 | English | en</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    parsed = _parse_sub_caption(caption_raw)
    if not parsed:
        await message.reply_text(
            "❌ <b>Could not parse subtitle caption.</b>\n\n"
            "Format: <code>AniList ID | Episode | Language Label | lang_code</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    anilist_id = parsed["anilist_id"]
    episode    = parsed["episode"]
    label      = parsed["label"]
    lang       = parsed["lang"]

    # Episode must already exist
    existing = await site_db.get_episode_qualities(anilist_id, episode)
    if not existing:
        await message.reply_text(
            f"❌ Episode not found.\n"
            f"Upload the video for AniList ID <code>{anilist_id}</code> "
            f"E{episode:02d} first.",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    # Fetch anime info so we have a valid name/slug for get_or_create
    anime_info = await fetch_anime_by_id(anilist_id)
    if anime_info:
        anime_id = await site_db.get_or_create_anime(
            anime_info["name"], anime_info["slug"], anilist_id
        )
        anime_name = anime_info["name"]
    else:
        # Fall back: look up by anilist_id already in DB
        row = await site_db.get_anime_by_anilist_id(anilist_id)
        if not row:
            await message.reply_text(
                f"❌ Anime (AniList ID <code>{anilist_id}</code>) not in DB.",
                parse_mode=ParseMode.HTML, quote=True
            )
            return
        anime_id   = row["id"]
        anime_name = row["name"]

    file_id = message.document.file_id
    sub_id  = await site_db.upsert_subtitle(
        anime_id=anime_id,
        episode=episode,
        label=label,
        lang=lang,
        file_id=file_id,
    )

    await message.reply_text(
        "✅ <b>Subtitle saved!</b>\n\n"
        f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
        f"<b>Episode:</b> {episode:02d}\n"
        f"<b>Track:</b> {label} ({lang})\n"
        f"<b>Subtitle ID:</b> <code>{sub_id}</code>\n\n"
        "It will appear automatically in the player for this episode.",
        parse_mode=ParseMode.HTML, quote=True
    )
