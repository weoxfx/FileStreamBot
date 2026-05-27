"""
Core plugin: receives video files, parses the caption as anime metadata,
applies the ffmpeg watermark, uploads to dump channel, stores in site DB,
and logs everything.

Caption format:
    Anime Name | Season | Episode | sub/dub/hsub | quality
Example:
    Naruto | 1 | 2 | sub | 720p
"""
import asyncio
import logging

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode, ChatType
from pyrogram.errors import FloodWait

from FileStream.bot import FileStream
from FileStream.config import Telegram, Site
from FileStream.utils.caption_parser import parse_caption, normalize_anime_name
from FileStream.utils.watermark import apply_watermark_and_upload
from FileStream.utils import bot_db, site_db

logger = logging.getLogger(__name__)

PROCESSING_MSG = (
    "⏳ <b>Processing your file...</b>\n"
    "Applying watermark and storing — this may take a while for large files."
)


def _is_video(message: Message) -> bool:
    return bool(message.video or message.document and
                message.document.mime_type and
                "video" in message.document.mime_type)


def _get_file_id(message: Message) -> str:
    if message.video:
        return message.video.file_id
    if message.document:
        return message.document.file_id
    return ""


def _get_file_size(message: Message) -> int:
    if message.video:
        return message.video.file_size or 0
    if message.document:
        return message.document.file_size or 0
    return 0


def _get_file_unique_id(message: Message) -> str:
    if message.video:
        return message.video.file_unique_id
    if message.document:
        return message.document.file_unique_id
    return ""


def _get_original_name(message: Message) -> str:
    if message.video and message.video.file_name:
        return message.video.file_name
    if message.document and message.document.file_name:
        return message.document.file_name
    return "video.mp4"


@FileStream.on_message(
    filters.private
    & (filters.video | filters.document)
    & filters.user(
        [Telegram.OWNER_ID] + list(Telegram.AUTH_USERS)
    ),
    group=1,
)
async def anime_file_handler(bot: Client, message: Message):
    """Handle incoming anime video files from authorized users."""
    caption_raw = message.caption or ""

    if not caption_raw or "|" not in caption_raw:
        await message.reply_text(
            "❌ <b>Missing or invalid caption.</b>\n\n"
            "Please send the file with a caption in this format:\n"
            "<code>Anime Name | Season | Episode | sub/dub/hsub | quality</code>\n\n"
            "Example:\n"
            "<code>Naruto | 1 | 2 | sub | 720p</code>",
            parse_mode=ParseMode.HTML,
            quote=True
        )
        return

    parsed = parse_caption(caption_raw)
    if not parsed:
        await message.reply_text(
            "❌ <b>Could not parse caption.</b>\n\n"
            "Format: <code>Anime Name | Season | Episode | sub/dub/hsub | quality</code>",
            parse_mode=ParseMode.HTML,
            quote=True
        )
        return

    if not _is_video(message):
        await message.reply_text(
            "❌ Only video files are supported for anime upload.",
            quote=True
        )
        return

    if not Telegram.DUMP_CHANNEL:
        await message.reply_text(
            "❌ DUMP_CHANNEL is not configured. Ask the bot admin to set it.",
            quote=True
        )
        return

    status_msg = await message.reply_text(PROCESSING_MSG, parse_mode=ParseMode.HTML, quote=True)

    anime_name = parsed["anime_name"]
    season = parsed["season"]
    episode = parsed["episode"]
    audio_type = parsed["audio_type"]
    quality = parsed["quality"]
    slug = normalize_anime_name(anime_name)

    file_id = _get_file_id(message)
    file_unique_id = _get_file_unique_id(message)
    file_size = _get_file_size(message)
    original_name = _get_original_name(message)

    dump_caption = (
        f"#{slug.replace('-', '_')} | S{season:02d}E{episode:02d} | "
        f"{audio_type.upper()} | {quality}\n"
        f"<b>{anime_name}</b>"
    )

    try:
        dump_msg_id, _ = await apply_watermark_and_upload(
            bot_client=bot,
            original_file_id=file_id,
            original_file_name=original_name,
            dump_channel_id=Telegram.DUMP_CHANNEL,
            caption=dump_caption,
        )
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
        dump_msg_id, _ = await apply_watermark_and_upload(
            bot_client=bot,
            original_file_id=file_id,
            original_file_name=original_name,
            dump_channel_id=Telegram.DUMP_CHANNEL,
            caption=dump_caption,
        )

    if not dump_msg_id:
        await status_msg.edit_text(
            "❌ Failed to process and upload the file. Check bot logs.",
            parse_mode=ParseMode.HTML
        )
        return

    user_id = message.from_user.id

    await bot_db.add_user(user_id)
    await bot_db.log_file(
        user_id=user_id,
        file_unique_id=file_unique_id,
        file_name=original_name,
        file_size=file_size,
        mime_type="video/mp4",
        dump_msg_id=dump_msg_id,
    )
    await bot_db.write_bot_log(
        "INFO",
        f"Uploaded: {anime_name} S{season}E{episode} [{audio_type}] [{quality}] "
        f"by user {user_id} → dump msg {dump_msg_id}"
    )

    anime_id = await site_db.get_or_create_anime(anime_name, slug)
    stream_token = await site_db.upsert_episode(
        anime_id=anime_id,
        season=season,
        episode=episode,
        audio_type=audio_type,
        quality=quality,
        dump_msg_id=dump_msg_id,
        dump_channel_id=Telegram.DUMP_CHANNEL,
        file_size=file_size,
        anime_slug=slug,
    )

    stream_url = f"http://localhost:5000/stream/{stream_token}"
    api_url = f"http://localhost:5000/api/episodes/{slug}?season={season}&episode={episode}"

    if Telegram.ULOG_CHANNEL:
        try:
            await bot.send_message(
                Telegram.ULOG_CHANNEL,
                f"✅ <b>#NewEpisode</b>\n"
                f"<b>Anime:</b> {anime_name}\n"
                f"<b>Season:</b> {season} | <b>Episode:</b> {episode}\n"
                f"<b>Type:</b> {audio_type.upper()} | <b>Quality:</b> {quality}\n"
                f"<b>Dump msg:</b> {dump_msg_id}\n"
                f"<b>Token:</b> <code>{stream_token}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await status_msg.edit_text(
        f"✅ <b>Done!</b>\n\n"
        f"<b>Anime:</b> {anime_name}\n"
        f"<b>Season {season} | Episode {episode}</b>\n"
        f"<b>Type:</b> {audio_type.upper()} | <b>Quality:</b> {quality}\n\n"
        f"<b>Stream Token:</b>\n<code>{stream_token}</code>\n\n"
        f"<b>Stream URL:</b>\n<code>{stream_url}</code>\n\n"
        f"<b>API (all qualities for this episode):</b>\n<code>{api_url}</code>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
