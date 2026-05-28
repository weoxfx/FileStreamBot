"""
Subtitle upload plugin.

Usage — send a .vtt or .srt file with a caption in this format:
    Anime Name | Season | Episode | Language Label | lang_code

Examples:
    Naruto | 1 | 2 | English | en
    Naruto | 1 | 2 | Arabic  | ar

The bot will store the file in Telegram and link it to the episode in the DB.
Subtitles are then served at /subtitle/<id> and auto-loaded by the player.
"""
import logging

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.utils.caption_parser import normalize_anime_name
from FileStream.utils import site_db

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = (".vtt", ".srt", ".ass", ".ssa")


def _parse_sub_caption(text: str):
    """
    Parse:  Anime Name | Season | Episode | Label | lang_code
    Returns dict or None.
    """
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 5:
        return None
    anime_name, season_s, episode_s, label, lang = parts
    try:
        season  = int(season_s)
        episode = int(episode_s)
    except ValueError:
        return None
    if not lang:
        return None
    return {
        "anime_name": anime_name,
        "season":     season,
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
            "Format:\n<code>Anime Name | Season | Episode | Language Label | lang_code</code>\n\n"
            "Example:\n<code>Naruto | 1 | 2 | English | en</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    parsed = _parse_sub_caption(caption_raw)
    if not parsed:
        await message.reply_text(
            "❌ <b>Could not parse subtitle caption.</b>\n\n"
            "Format: <code>Anime Name | Season | Episode | Language Label | lang_code</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    anime_name = parsed["anime_name"]
    season     = parsed["season"]
    episode    = parsed["episode"]
    label      = parsed["label"]
    lang       = parsed["lang"]
    slug       = normalize_anime_name(anime_name)

    # Anime must already exist in the DB
    existing_qualities = await site_db.get_episode_qualities(slug, season, episode)
    if not existing_qualities:
        await message.reply_text(
            f"❌ Episode not found in DB.\n"
            f"Make sure the video for <b>{anime_name} S{season:02d}E{episode:02d}</b> "
            f"has already been uploaded first.",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    file_id  = message.document.file_id
    anime_id = await site_db.get_or_create_anime(anime_name, slug)

    sub_id = await site_db.upsert_subtitle(
        anime_id=anime_id,
        season=season,
        episode=episode,
        label=label,
        lang=lang,
        file_id=file_id,
    )

    await message.reply_text(
        "✅ <b>Subtitle saved!</b>\n\n"
        f"<b>Anime:</b> {anime_name}\n"
        f"<b>Episode:</b> S{season:02d}E{episode:02d}\n"
        f"<b>Track:</b> {label} ({lang})\n"
        f"<b>Subtitle ID:</b> <code>{sub_id}</code>\n\n"
        "It will appear automatically in the player for this episode.",
        parse_mode=ParseMode.HTML, quote=True
    )
