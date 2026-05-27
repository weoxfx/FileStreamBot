"""
Core plugin: receives video files with anime captions, watermarks via ffmpeg,
uploads to dump channel, stores in site DB, shows live download/upload progress.

Caption format:
    Anime Name | Season | Episode | sub/dub/hsub | quality
Example:
    Naruto | 1 | 2 | sub | 720p
"""
import asyncio
import logging
import time as time_mod

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified

from FileStream.bot import FileStream
from FileStream.config import Telegram, Server
from FileStream.utils.caption_parser import parse_caption, normalize_anime_name
from FileStream.utils.watermark import apply_watermark_and_upload
from FileStream.utils import bot_db, site_db
from FileStream.utils.human_readable import humanbytes

logger = logging.getLogger(__name__)

BAR_LEN = 16


def _make_bar(current, total):
    if not total:
        return "░" * BAR_LEN
    filled = int(BAR_LEN * current / total)
    return "█" * filled + "░" * (BAR_LEN - filled)


def _is_video(message: Message) -> bool:
    if message.video:
        return True
    if message.document and message.document.mime_type:
        return "video" in message.document.mime_type
    return False


def _get_field(message: Message, field: str, default=""):
    for attr in ("video", "document"):
        media = getattr(message, attr, None)
        if media:
            return getattr(media, field, default) or default
    return default


@FileStream.on_message(
    filters.private
    & (filters.video | filters.document)
    & filters.user([Telegram.OWNER_ID] + list(Telegram.AUTH_USERS)),
    group=1,
)
async def anime_file_handler(bot: Client, message: Message):
    caption_raw = (message.caption or "").strip()

    if not caption_raw or "|" not in caption_raw:
        await message.reply_text(
            "❌ <b>Missing or invalid caption.</b>\n\n"
            "Format:\n<code>Anime Name | Season | Episode | sub/dub/hsub | quality</code>\n\n"
            "Example:\n<code>Naruto | 1 | 2 | sub | 720p</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    parsed = parse_caption(caption_raw)
    if not parsed:
        await message.reply_text(
            "❌ <b>Could not parse caption.</b>\n\n"
            "Format: <code>Anime Name | Season | Episode | sub/dub/hsub | quality</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    if not _is_video(message):
        await message.reply_text("❌ Only video files are supported.", quote=True)
        return

    if not Telegram.DUMP_CHANNEL:
        await message.reply_text("❌ DUMP_CHANNEL is not configured.", quote=True)
        return

    anime_name = parsed["anime_name"]
    season     = parsed["season"]
    episode    = parsed["episode"]
    audio_type = parsed["audio_type"]
    quality    = parsed["quality"]
    slug       = normalize_anime_name(anime_name)

    file_id        = _get_field(message, "file_id")
    file_unique_id = _get_field(message, "file_unique_id")
    file_size      = int(_get_field(message, "file_size", 0))
    original_name  = _get_field(message, "file_name") or "video.mp4"
    user_id        = message.from_user.id

    status_msg = await message.reply_text(
        f"⬇️ <b>Downloading…</b>\n"
        f"<code>[{'░' * BAR_LEN}]</code> 0%\n"
        f"<b>{anime_name}</b> S{season:02d}E{episode:02d} [{audio_type.upper()}] [{quality}]",
        parse_mode=ParseMode.HTML, quote=True
    )

    _last_dl_edit = [0.0]

    async def dl_progress(current, total):
        now = time_mod.time()
        if now - _last_dl_edit[0] < 3.5:
            return
        _last_dl_edit[0] = now
        pct = int(current * 100 / total) if total else 0
        bar = _make_bar(current, total)
        try:
            await status_msg.edit_text(
                f"⬇️ <b>Downloading…</b>\n"
                f"<code>[{bar}]</code> {pct}%\n"
                f"{humanbytes(current)} / {humanbytes(total)}\n\n"
                f"<b>{anime_name}</b> S{season:02d}E{episode:02d} [{audio_type.upper()}] [{quality}]",
                parse_mode=ParseMode.HTML
            )
        except (MessageNotModified, Exception):
            pass

    # Show watermarking status after download completes
    async def after_download():
        try:
            await status_msg.edit_text(
                f"🎨 <b>Applying Tsukuyomi watermark…</b>\n"
                f"This may take a few minutes for large files.\n\n"
                f"<b>{anime_name}</b> S{season:02d}E{episode:02d} [{audio_type.upper()}] [{quality}]",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    try:
        # We patch: call watermark util with progress, then signal watermarking phase
        dump_msg_id, _ = await _watermark_with_phases(
            bot=bot,
            file_id=file_id,
            original_name=original_name,
            dump_channel_id=Telegram.DUMP_CHANNEL,
            dump_caption=(
                f"#{slug.replace('-','_')} | S{season:02d}E{episode:02d} | "
                f"{audio_type.upper()} | {quality}\n"
                f"<b>{anime_name}</b>"
            ),
            dl_progress=dl_progress,
            after_download_cb=after_download,
            status_msg=status_msg,
            anime_name=anime_name,
            season=season,
            episode=episode,
            audio_type=audio_type,
            quality=quality,
        )
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
        dump_msg_id = None

    if not dump_msg_id:
        await status_msg.edit_text(
            "❌ <b>Failed to process/upload the file.</b>\nCheck bot logs for details.",
            parse_mode=ParseMode.HTML
        )
        return

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
        "Uploaded {} S{}E{} [{}] [{}] by {} → dump msg {}".format(
            anime_name, season, episode, audio_type, quality, user_id, dump_msg_id
        )
    )

    anime_id     = await site_db.get_or_create_anime(anime_name, slug)
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

    base = Server.URL.rstrip("/")
    stream_url = "{}/stream/{}".format(base, stream_token)
    player_url = "{}/player/{}".format(base, stream_token)

    if Telegram.ULOG_CHANNEL:
        try:
            await bot.send_message(
                Telegram.ULOG_CHANNEL,
                "✅ <b>#NewEpisode</b>\n"
                "<b>Anime:</b> {}\n"
                "<b>Season:</b> {} | <b>Episode:</b> {}\n"
                "<b>Type:</b> {} | <b>Quality:</b> {}\n"
                "<b>Token:</b> <code>{}</code>".format(
                    anime_name, season, episode,
                    audio_type.upper(), quality, stream_token
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await status_msg.edit_text(
        "✅ <b>Done!</b>\n\n"
        "<b>Anime:</b> {}\n"
        "<b>Season {} | Episode {}</b>\n"
        "<b>Type:</b> {} | <b>Quality:</b> {}\n\n"
        "<b>Stream Token:</b>\n<code>{}</code>\n\n"
        "<b>Player URL:</b>\n<code>{}</code>\n\n"
        "<b>Stream URL:</b>\n<code>{}</code>".format(
            anime_name, season, episode,
            audio_type.upper(), quality,
            stream_token, player_url, stream_url
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _watermark_with_phases(
    bot, file_id, original_name, dump_channel_id, dump_caption,
    dl_progress, after_download_cb, status_msg,
    anime_name, season, episode, audio_type, quality
):
    """Wraps apply_watermark_and_upload with a mid-point callback after download."""
    import os, tempfile, asyncio
    from FileStream.utils.watermark import _run_ffmpeg

    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        safe_name = original_name.replace("/", "_").replace("\\", "_")
        raw_path = os.path.join(tmpdir, "raw_" + safe_name)
        wm_path  = os.path.join(tmpdir, "wm_"  + safe_name)

        try:
            dl_path = await bot.download_media(
                file_id,
                file_name=raw_path,
                progress=dl_progress,
            )
        except Exception as e:
            logger.error("Download error: %s", e)
            return None, None

        if not dl_path or not os.path.exists(dl_path):
            return None, None

        await after_download_cb()

        ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
        upload_path = wm_path if (ok and os.path.exists(wm_path)) else dl_path

        try:
            await status_msg.edit_text(
                "⬆️ <b>Uploading to dump channel…</b>\n\n"
                "<b>{}</b> S{:02d}E{:02d} [{}] [{}]".format(
                    anime_name, season, episode, audio_type.upper(), quality
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        try:
            sent = await bot.send_video(
                chat_id=dump_channel_id,
                video=upload_path,
                caption=dump_caption,
                supports_streaming=True,
            )
            media = getattr(sent, "video", None) or getattr(sent, "document", None)
            return sent.id, getattr(media, "file_id", None)
        except Exception as e:
            logger.error("Upload error: %s", e)
            return None, None
