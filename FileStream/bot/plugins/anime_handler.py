"""
Core upload plugin — receives video files, watermarks via ffmpeg,
uploads to dump channel, stores in site DB, shows live progress.

Modes (set with /mode):
  anilist_id  — caption: "AniList ID | Episode | sub/dub/hsub | quality"
  auto_sub    — filename: "Show Name - Episode - Quality.ext"  (audio=sub)
  auto_dub    — filename: "Show Name - Episode - Quality.ext"  (audio=dub)
"""
import os
import asyncio
import logging
import tempfile
import time as time_mod
from typing import Dict

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified

from FileStream.bot import FileStream
from FileStream.config import Telegram, Server
from FileStream.utils.caption_parser import parse_caption, parse_filename
from FileStream.utils.watermark import _run_ffmpeg
from FileStream.utils import bot_db, site_db
from FileStream.utils.anilist import fetch_anime_by_id, search_anime_by_name
from FileStream.utils.human_readable import humanbytes

logger = logging.getLogger(__name__)

BAR_LEN = 16

# Active upload tasks: message_id → asyncio.Task
# Used by /stop to cancel in-flight uploads.
_active_tasks: Dict[int, asyncio.Task] = {}


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
    if not _is_video(message):
        return

    if not Telegram.DUMP_CHANNEL:
        await message.reply_text("❌ DUMP_CHANNEL is not configured.", quote=True)
        return

    task = asyncio.ensure_future(_process_upload(bot, message))
    _active_tasks[message.id] = task

    def _done(t):
        _active_tasks.pop(message.id, None)

    task.add_done_callback(_done)


async def _process_upload(bot: Client, message: Message):
    mode = await bot_db.get_upload_mode()

    if mode == "anilist_id":
        await _handle_anilist_mode(bot, message)
    elif mode in ("auto_sub", "auto_dub"):
        audio_type = "sub" if mode == "auto_sub" else "dub"
        await _handle_auto_mode(bot, message, audio_type)
    else:
        await _handle_anilist_mode(bot, message)


# ── AniList ID mode ──────────────────────────────────────────────────────────

async def _handle_anilist_mode(bot: Client, message: Message):
    caption_raw = (message.caption or "").strip()

    if not caption_raw or "|" not in caption_raw:
        await message.reply_text(
            "❌ <b>Missing caption.</b>\n\n"
            "<b>Format:</b>\n<code>AniList ID | Episode | sub/dub/hsub | quality</code>\n\n"
            "<b>Example:</b>\n<code>21355 | 1 | sub | 720p</code>\n\n"
            "Use /mode to switch to filename-based auto mode.",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    parsed = parse_caption(caption_raw)
    if not parsed:
        await message.reply_text(
            "❌ <b>Could not parse caption.</b>\n\n"
            "<b>Format:</b> <code>AniList ID | Episode | sub/dub/hsub | quality</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    status_msg = await message.reply_text(
        "🔍 <b>Looking up AniList ID…</b>", parse_mode=ParseMode.HTML, quote=True
    )

    anime_info = await fetch_anime_by_id(parsed["anilist_id"])
    if not anime_info:
        await status_msg.edit_text(
            f"❌ <b>AniList ID {parsed['anilist_id']} not found.</b>\n"
            "Check the ID at anilist.co",
            parse_mode=ParseMode.HTML
        )
        return

    await _do_upload(
        bot=bot,
        message=message,
        status_msg=status_msg,
        anime_info=anime_info,
        episode=parsed["episode"],
        audio_type=parsed["audio_type"],
        quality=parsed["quality"],
    )


# ── Auto mode (filename parsing) ─────────────────────────────────────────────

async def _handle_auto_mode(bot: Client, message: Message, audio_type: str):
    filename = _get_field(message, "file_name") or ""
    if not filename:
        if message.video:
            filename = f"video_{message.id}.mp4"
        else:
            await message.reply_text(
                "❌ <b>Could not read filename.</b>\nSend the file as a document, not a compressed video.",
                parse_mode=ParseMode.HTML, quote=True
            )
            return

    parsed = parse_filename(filename)
    if not parsed:
        await message.reply_text(
            "❌ <b>Filename does not match the expected pattern.</b>\n\n"
            "<b>Expected:</b> <code>Show Name - Episode - Quality.ext</code>\n"
            "<b>Example:</b> <code>ReZERO -Starting Life in Another World- - 1 - 360p.mkv</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    status_msg = await message.reply_text(
        f"🔍 <b>Searching AniList for:</b> <code>{parsed['anime_name']}</code>",
        parse_mode=ParseMode.HTML, quote=True
    )

    anime_info = await search_anime_by_name(parsed["anime_name"])
    if not anime_info:
        await status_msg.edit_text(
            f"❌ <b>Could not find anime on AniList:</b> <code>{parsed['anime_name']}</code>\n\n"
            "Try renaming the file or use AniList ID mode.",
            parse_mode=ParseMode.HTML
        )
        return

    await status_msg.edit_text(
        f"✅ <b>Found:</b> {anime_info['name']} <code>(ID: {anime_info['anilist_id']})</code>\n"
        f"📦 <b>Processing episode {parsed['episode']}…</b>",
        parse_mode=ParseMode.HTML
    )

    await _do_upload(
        bot=bot,
        message=message,
        status_msg=status_msg,
        anime_info=anime_info,
        episode=parsed["episode"],
        audio_type=audio_type,
        quality=parsed["quality"],
    )


# ── Core upload ──────────────────────────────────────────────────────────────

async def _do_upload(
    bot: Client,
    message: Message,
    status_msg,
    anime_info: dict,
    episode: int,
    audio_type: str,
    quality: str,
):
    anilist_id = anime_info["anilist_id"]
    anime_name = anime_info["name"]
    slug       = anime_info["slug"]

    file_id        = _get_field(message, "file_id")
    file_unique_id = _get_field(message, "file_unique_id")
    file_size      = int(_get_field(message, "file_size", 0))
    original_name  = _get_field(message, "file_name") or "video.mp4"
    user_id        = message.from_user.id

    # ── Duplicate guard ──────────────────────────────────────────────────────
    existing_qualities = await site_db.get_episode_qualities(anilist_id, episode)
    existing = next(
        (q for q in existing_qualities
         if q["audio_type"] == audio_type and q["quality"] == quality),
        None,
    )
    if existing:
        base = Server.URL.rstrip("/")
        await status_msg.edit_text(
            "⚠️ <b>Already exists — skipping duplicate.</b>\n\n"
            "<b>Anime:</b> {} <code>({})</code>\n"
            "<b>Episode:</b> {} | <b>Type:</b> {} | <b>Quality:</b> {}\n\n"
            "<b>Player:</b>\n<code>{}/player/{}</code>".format(
                anime_name, anilist_id, episode,
                audio_type.upper(), quality,
                base, existing["stream_token"]
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # ── Download → Watermark → Upload ────────────────────────────────────────
    _last_edit = [0.0]

    async def dl_progress(current, total):
        now = time_mod.time()
        if now - _last_edit[0] < 3.5:
            return
        _last_edit[0] = now
        pct = int(current * 100 / total) if total else 0
        bar = _make_bar(current, total)
        try:
            await status_msg.edit_text(
                "⬇️ <b>Downloading…</b>\n"
                "<code>[{}]</code> {}%\n"
                "{} / {}\n\n"
                "<b>{}</b> E{:02d} [{}] [{}]".format(
                    bar, pct,
                    humanbytes(current), humanbytes(total),
                    anime_name, episode, audio_type.upper(), quality
                ),
                parse_mode=ParseMode.HTML
            )
        except (MessageNotModified, Exception):
            pass

    loop = asyncio.get_event_loop()
    dump_msg_id = None

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            safe_name = original_name.replace("/", "_").replace("\\", "_")
            if not safe_name.lower().endswith(".mp4"):
                safe_name = os.path.splitext(safe_name)[0] + ".mp4"
            raw_path = os.path.join(tmpdir, "raw_" + safe_name)
            wm_path  = os.path.join(tmpdir, "wm_"  + safe_name)

            dl_path = await bot.download_media(
                file_id, file_name=raw_path, progress=dl_progress
            )
            if not dl_path or not os.path.exists(dl_path):
                raise RuntimeError("Download returned no file")

            try:
                await status_msg.edit_text(
                    "🎨 <b>Applying Tsukuyomi watermark…</b>\n\n"
                    "<b>{}</b> E{:02d} [{}] [{}]".format(
                        anime_name, episode, audio_type.upper(), quality
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

            ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
            upload_path = wm_path if (ok and os.path.exists(wm_path)) else dl_path

            try:
                await status_msg.edit_text(
                    "⬆️ <b>Uploading to dump channel…</b>\n\n"
                    "<b>{}</b> E{:02d} [{}] [{}]".format(
                        anime_name, episode, audio_type.upper(), quality
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

            dump_caption = (
                "#{} | E{:02d} | {} | {}\n<b>{}</b> (AniList: {})".format(
                    slug.replace("-", "_"), episode,
                    audio_type.upper(), quality,
                    anime_name, anilist_id
                )
            )
            sent = await bot.send_video(
                chat_id=Telegram.DUMP_CHANNEL,
                video=upload_path,
                caption=dump_caption,
                supports_streaming=True,
            )
            dump_msg_id = sent.id

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text(
                "🛑 <b>Upload cancelled by /stop.</b>", parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
    except Exception as e:
        logger.error("Upload pipeline error: %s", e, exc_info=True)
        try:
            await status_msg.edit_text(
                "❌ <b>Failed to process/upload the file.</b>\nCheck bot logs.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    if not dump_msg_id:
        try:
            await status_msg.edit_text(
                "❌ <b>Upload failed.</b>", parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    # ── Save to DB ───────────────────────────────────────────────────────────
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
        "Uploaded {} E{} [{}] [{}] by {} → dump {}".format(
            anime_name, episode, audio_type, quality, user_id, dump_msg_id
        )
    )

    anime_id     = await site_db.get_or_create_anime(anime_name, slug, anilist_id)
    stream_token = await site_db.upsert_episode(
        anime_id=anime_id,
        episode=episode,
        audio_type=audio_type,
        quality=quality,
        dump_msg_id=dump_msg_id,
        dump_channel_id=Telegram.DUMP_CHANNEL,
        file_size=file_size,
        anilist_id=anilist_id,
    )

    base       = Server.URL.rstrip("/")
    player_url = "{}/player/{}".format(base, stream_token)
    stream_url = "{}/stream/{}".format(base, stream_token)

    if Telegram.ULOG_CHANNEL:
        try:
            await bot.send_message(
                Telegram.ULOG_CHANNEL,
                "✅ <b>#NewEpisode</b>\n"
                "<b>Anime:</b> {} <code>({})</code>\n"
                "<b>Episode:</b> {} | <b>Type:</b> {} | <b>Quality:</b> {}\n"
                "<b>Token:</b> <code>{}</code>".format(
                    anime_name, anilist_id, episode,
                    audio_type.upper(), quality, stream_token
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await status_msg.edit_text(
        "✅ <b>Done!</b>\n\n"
        "<b>Anime:</b> {} <code>({})</code>\n"
        "<b>Episode:</b> {} | <b>Type:</b> {} | <b>Quality:</b> {}\n\n"
        "<b>Token:</b>\n<code>{}</code>\n\n"
        "<b>Player:</b>\n<code>{}</code>".format(
            anime_name, anilist_id, episode,
            audio_type.upper(), quality,
            stream_token, player_url
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
