"""
Core upload plugin — receives video files, watermarks via ffmpeg,
uploads to dump channel, stores in site DB, shows live progress.

Modes (set with /mode):
  anilist_id  — caption: "AniList ID | Episode | sub/dub/hsub | quality"
  auto_sub    — caption first (AniList ID format), then filename fallback (audio=sub)
  auto_dub    — caption first (AniList ID format), then filename fallback (audio=dub)

In auto_sub/auto_dub: if caption parses as valid AniList ID format, caption wins
(its explicit audio type overrides the mode). Otherwise filename is parsed.
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

# Active upload tasks: message_id → asyncio.Task  (used by /stop)
_active_tasks: Dict[int, asyncio.Task] = {}

# Video MIME types and extensions we accept
_VIDEO_MIMES = {"video/mp4", "video/x-matroska", "video/webm", "video/x-msvideo",
                "video/quicktime", "video/x-flv", "video/MP2T", "video/mp2t"}
_VIDEO_EXTS  = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}

# Max FloodWait retries for the dump-channel send
_MAX_FLOOD_RETRIES = 3


def _make_bar(current, total):
    if not total:
        return "░" * BAR_LEN
    filled = int(BAR_LEN * current / total)
    return "█" * filled + "░" * (BAR_LEN - filled)


def _is_video(message: Message) -> bool:
    if message.video:
        return True
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime in _VIDEO_MIMES or "video" in mime:
            return True
        fname = (message.document.file_name or "").lower()
        _, ext = os.path.splitext(fname)
        if ext in _VIDEO_EXTS:
            return True
    return False


def _get_media(message: Message):
    """Return the media object (video or document), or None."""
    return message.video or message.document or None


def _get_field(message: Message, field: str, default=None):
    """Safely get a field from video or document media."""
    media = _get_media(message)
    if media:
        return getattr(media, field, default)
    return default


def _safe_hashtag(slug: str) -> str:
    """Strip characters that break Telegram hashtags (keep alphanumeric + underscore)."""
    return re.sub(r"[^\w]", "_", slug, flags=re.ASCII)


# Need re for _safe_hashtag
import re


_SUBTITLE_EXTS = {".vtt", ".srt", ".ass", ".ssa"}


def _is_subtitle(message: Message) -> bool:
    if message.document:
        fname = (message.document.file_name or "").lower()
        _, ext = os.path.splitext(fname)
        if ext in _SUBTITLE_EXTS:
            return True
    return False


@FileStream.on_message(
    filters.private
    & (filters.video | filters.document)
    & filters.user([Telegram.OWNER_ID] + list(Telegram.AUTH_USERS)),
    group=1,
)
async def anime_file_handler(bot: Client, message: Message):
    if _is_subtitle(message):
        # Route subtitle file uploads to the subtitle handler
        task = asyncio.ensure_future(_process_subtitle_upload(bot, message))
        _active_tasks[message.id] = task
        task.add_done_callback(lambda t: _active_tasks.pop(message.id, None))
        return

    if not _is_video(message):
        return

    if not Telegram.DUMP_CHANNEL:
        await message.reply_text("❌ DUMP_CHANNEL is not configured.", quote=True)
        return

    task = asyncio.ensure_future(_process_upload(bot, message))
    _active_tasks[message.id] = task
    task.add_done_callback(lambda t: _active_tasks.pop(message.id, None))


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
            "Use /mode to switch to filename auto mode.",
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
        bot=bot, message=message, status_msg=status_msg,
        anime_info=anime_info,
        episode=parsed["episode"],
        audio_type=parsed["audio_type"],
        quality=parsed["quality"],
    )


# ── Auto mode (caption first, then filename) ─────────────────────────────────

async def _handle_auto_mode(bot: Client, message: Message, default_audio: str):
    """
    Priority order:
      1. Caption as AniList ID format  "21355 | 1 | sub | 720p"
      2. Caption as filename format    "Show Name - 1 - 720p.mkv"
      3. Actual file_name field        "Show Name - 1 - 720p.mkv"
    """
    caption_raw = (message.caption or "").strip()

    # ── Step 1: caption as AniList ID format ─────────────────────────────────
    if caption_raw and "|" in caption_raw:
        parsed = parse_caption(caption_raw)
        if parsed:
            status_msg = await message.reply_text(
                "🔍 <b>Looking up AniList ID from caption…</b>",
                parse_mode=ParseMode.HTML, quote=True
            )
            anime_info = await fetch_anime_by_id(parsed["anilist_id"])
            if anime_info:
                await _do_upload(
                    bot=bot, message=message, status_msg=status_msg,
                    anime_info=anime_info,
                    episode=parsed["episode"],
                    audio_type=parsed["audio_type"],  # caption's explicit type wins
                    quality=parsed["quality"],
                )
                return
            await status_msg.edit_text(
                f"⚠️ AniList ID <code>{parsed['anilist_id']}</code> not found.\n"
                "Trying filename…",
                parse_mode=ParseMode.HTML
            )
            return await _handle_auto_filename(
                bot, message, default_audio, status_msg=status_msg
            )

    # ── Step 2: caption as filename format ───────────────────────────────────
    if caption_raw:
        fn_parsed = parse_filename(caption_raw)
        if fn_parsed:
            status_msg = await message.reply_text(
                f"🔍 <b>Searching AniList for:</b> <code>{fn_parsed['anime_name']}</code>\n"
                "<i>(from caption)</i>",
                parse_mode=ParseMode.HTML, quote=True
            )
            anime_info = await search_anime_by_name(fn_parsed["anime_name"])
            if anime_info:
                await _do_upload(
                    bot=bot, message=message, status_msg=status_msg,
                    anime_info=anime_info,
                    episode=fn_parsed["episode"],
                    audio_type=default_audio,
                    quality=fn_parsed["quality"],
                )
                return
            await status_msg.edit_text(
                f"⚠️ <b>Could not find on AniList:</b> <code>{fn_parsed['anime_name']}</code>\n"
                "Trying file's actual name…",
                parse_mode=ParseMode.HTML
            )
            return await _handle_auto_filename(
                bot, message, default_audio, status_msg=status_msg
            )

    # ── Step 3: actual file_name field ───────────────────────────────────────
    await _handle_auto_filename(bot, message, default_audio)


async def _handle_auto_filename(
    bot: Client,
    message: Message,
    default_audio: str,
    status_msg=None,
):
    """Parse filename and continue the upload. Reuses status_msg if provided."""

    filename = _get_field(message, "file_name") or ""
    if not filename:
        txt = (
            "❌ <b>No caption and no readable filename.</b>\n\n"
            "Either add a caption <code>AniList ID | ep | sub | quality</code>\n"
            "or send the file with a name like:\n"
            "<code>Show Name - 1 - 720p.mkv</code>"
        )
        if status_msg:
            await status_msg.edit_text(txt, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(txt, parse_mode=ParseMode.HTML, quote=True)
        return

    fn_parsed = parse_filename(filename)
    if not fn_parsed:
        txt = (
            "❌ <b>Filename does not match expected pattern.</b>\n\n"
            "<b>Expected:</b> <code>Show Name - Episode - Quality.ext</code>\n"
            "<b>Example:</b> <code>ReZERO -Starting Life in Another World- - 1 - 360p.mkv</code>\n\n"
            f"<b>Got:</b> <code>{filename}</code>"
        )
        if status_msg:
            await status_msg.edit_text(txt, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(txt, parse_mode=ParseMode.HTML, quote=True)
        return

    search_txt = (
        f"🔍 <b>Searching AniList for:</b> <code>{fn_parsed['anime_name']}</code>"
    )
    if status_msg:
        await status_msg.edit_text(search_txt, parse_mode=ParseMode.HTML)
    else:
        status_msg = await message.reply_text(
            search_txt, parse_mode=ParseMode.HTML, quote=True
        )

    anime_info = await search_anime_by_name(fn_parsed["anime_name"])
    if not anime_info:
        await status_msg.edit_text(
            f"❌ <b>Could not find on AniList:</b> <code>{fn_parsed['anime_name']}</code>\n\n"
            "Try renaming the file or use AniList ID mode (/mode).",
            parse_mode=ParseMode.HTML
        )
        return

    await status_msg.edit_text(
        f"✅ <b>Found:</b> {anime_info['name']} <code>(ID: {anime_info['anilist_id']})</code>\n"
        f"📦 <b>Processing episode {fn_parsed['episode']}…</b>",
        parse_mode=ParseMode.HTML
    )

    await _do_upload(
        bot=bot, message=message, status_msg=status_msg,
        anime_info=anime_info,
        episode=fn_parsed["episode"],
        audio_type=default_audio,
        quality=fn_parsed["quality"],
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
    anilist_id     = anime_info["anilist_id"]
    anime_name     = anime_info["name"]
    slug           = anime_info["slug"]
    mal_id         = anime_info.get("mal_id")
    cover_url      = anime_info.get("cover_url") or ""
    synopsis       = anime_info.get("synopsis") or ""
    total_episodes = anime_info.get("total_episodes")

    file_id        = _get_field(message, "file_id") or ""
    file_unique_id = _get_field(message, "file_unique_id") or ""
    # FIX: use explicit default=0 and guard against None to avoid int("") crash
    file_size      = int(_get_field(message, "file_size") or 0)
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
            # FIX: preserve original extension rather than hardcoding .mp4
            _, original_ext = os.path.splitext(original_name)
            if not original_ext or original_ext.lower() not in _VIDEO_EXTS:
                original_ext = ".mp4"  # safe fallback

            safe_base = re.sub(r'[/\\]', '_', os.path.splitext(original_name)[0])
            safe_name = safe_base + original_ext

            raw_path = os.path.join(tmpdir, "raw_" + safe_name)
            # Watermarked output is always .mp4 (ffmpeg transcode target)
            wm_name  = safe_base + ".mp4"
            wm_path  = os.path.join(tmpdir, "wm_" + wm_name)

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
                    _safe_hashtag(slug), episode,
                    audio_type.upper(), quality,
                    anime_name, anilist_id
                )
            )

            # FIX: retry on FloodWait instead of silently failing
            sent = await _send_with_flood_retry(
                bot=bot,
                chat_id=Telegram.DUMP_CHANNEL,
                video=upload_path,
                caption=dump_caption,
                status_msg=status_msg,
            )
            if sent is None:
                return  # error already reported to user
            dump_msg_id = sent.id

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text(
                "🛑 <b>Upload cancelled by /stop.</b>", parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return
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
            await status_msg.edit_text("❌ <b>Upload failed.</b>", parse_mode=ParseMode.HTML)
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

    anime_id = await site_db.get_or_create_anime(
        anime_name, slug, anilist_id,
        mal_id=mal_id,
        cover_url=cover_url,
        synopsis=synopsis,
        total_episodes=total_episodes,
    )
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

    if Telegram.ULOG_CHANNEL:
        try:
            score_txt = f" • ⭐ {anime_info.get('score')}" if anime_info.get("score") else ""
            await bot.send_message(
                Telegram.ULOG_CHANNEL,
                "✅ <b>#NewEpisode</b>\n"
                "<b>Anime:</b> {} <code>({})</code>{}\n"
                "<b>Episode:</b> {} | <b>Type:</b> {} | <b>Quality:</b> {}\n"
                "<b>Token:</b> <code>{}</code>".format(
                    anime_name, anilist_id, score_txt, episode,
                    audio_type.upper(), quality, stream_token
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    genres_txt = ", ".join(anime_info.get("genres", [])[:3])
    await status_msg.edit_text(
        "✅ <b>Done!</b>\n\n"
        "<b>Anime:</b> {} <code>({})</code>\n"
        "{}"
        "<b>Episode:</b> {} | <b>Type:</b> {} | <b>Quality:</b> {}\n\n"
        "<b>Token:</b>\n<code>{}</code>\n\n"
        "<b>Player:</b>\n<code>{}</code>".format(
            anime_name, anilist_id,
            f"<b>Genres:</b> {genres_txt}\n" if genres_txt else "",
            episode, audio_type.upper(), quality,
            stream_token, player_url
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _send_with_flood_retry(bot, chat_id, video, caption, status_msg):
    """
    Send video to dump channel, retrying up to _MAX_FLOOD_RETRIES times on
    FloodWait. Returns the sent Message or None on failure.
    """
    for attempt in range(1, _MAX_FLOOD_RETRIES + 1):
        try:
            return await bot.send_video(
                chat_id=chat_id,
                video=video,
                caption=caption,
                supports_streaming=True,
            )
        except FloodWait as fw:
            if attempt == _MAX_FLOOD_RETRIES:
                logger.error("FloodWait exceeded max retries (%ds)", fw.value)
                try:
                    await status_msg.edit_text(
                        f"❌ <b>Telegram rate limit hit ({fw.value}s). Upload aborted.</b>\n"
                        "Try again in a few minutes.",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return None
            logger.warning(
                "FloodWait %ds on attempt %d/%d — sleeping…",
                fw.value, attempt, _MAX_FLOOD_RETRIES
            )
            try:
                await status_msg.edit_text(
                    f"⏳ <b>Telegram rate limit — waiting {fw.value}s…</b>\n"
                    f"(attempt {attempt}/{_MAX_FLOOD_RETRIES})",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await asyncio.sleep(fw.value)
        except Exception:
            raise  # let the caller's try/except handle non-FloodWait errors


# ── Subtitle upload ───────────────────────────────────────────────────────────

_SUB_CAPTION_RE = re.compile(
    r"^\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*([a-zA-Z]{2,5})\s*$"
)


def _parse_sub_caption(caption: str):
    """Parse 'AniList ID | Episode | Language Label | lang_code' caption."""
    m = _SUB_CAPTION_RE.match(caption)
    if not m:
        return None
    return {
        "anilist_id": int(m.group(1)),
        "episode":    int(m.group(2)),
        "label":      m.group(3).strip(),
        "lang":       m.group(4).strip().lower(),
    }


def _run_hardsub_ffmpeg(video_path: str, sub_path: str, output_path: str) -> bool:
    """Burn subtitle into video using ffmpeg. Returns True on success."""
    import subprocess, os
    if not os.path.exists(video_path) or not os.path.exists(sub_path):
        return False

    _, ext = os.path.splitext(sub_path.lower())
    if ext in (".ass", ".ssa"):
        vf = f"ass={sub_path}"
    else:
        # SRT, VTT and others — use subtitles filter (may need conversion)
        vf = f"subtitles={sub_path}"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-c:a", "copy",
        "-sn",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        logger.warning("Hard-sub ffmpeg failed (rc=%d): %s", r.returncode,
                        r.stderr.decode(errors="replace")[-500:])
    except subprocess.TimeoutExpired:
        logger.error("Hard-sub ffmpeg timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
    return False


def _run_softsub_ffmpeg(video_path: str, sub_path: str, output_path: str, lang: str = "eng") -> bool:
    """
    Mux subtitle as a soft subtitle track into an mp4 container.
    The video and audio streams are copied without re-encoding (fast).
    Returns True on success.
    """
    import subprocess, os
    if not os.path.exists(video_path) or not os.path.exists(sub_path):
        return False

    # Normalize 2-char lang code to 3-char ISO 639-2 for Telegram compatibility
    _lang2to3 = {
        "en": "eng", "ja": "jpn", "fr": "fra", "de": "deu",
        "es": "spa", "pt": "por", "it": "ita", "ru": "rus",
        "zh": "zho", "ko": "kor", "ar": "ara", "pl": "pol",
    }
    lang3 = _lang2to3.get(lang.lower()[:2], lang[:3].lower())

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", sub_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={lang3}",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        logger.warning("Soft-sub ffmpeg failed (rc=%d): %s", r.returncode,
                        r.stderr.decode(errors="replace")[-500:])
    except subprocess.TimeoutExpired:
        logger.error("Soft-sub ffmpeg timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
    return False


async def _process_subtitle_upload(bot: Client, message: Message):
    """
    Handle subtitle file (.vtt/.srt/.ass/.ssa) uploads.

    Caption format: AniList ID | Episode | Language Label | lang_code
    Example:        21355 | 1 | English | en

    Flow:
      1. Store subtitle file_id in DB (soft sub — available to all episode variants).
      2. Create a hard-subbed mp4 (subtitles burned in)  → upload as audio_type='hsub'.
      3. Create a soft-subbed mp4 (subtitle muxed as track) → upload as audio_type='sub'.
      Both give independent stream tokens.
    """
    caption_raw = (message.caption or "").strip()
    parsed = _parse_sub_caption(caption_raw) if caption_raw else None

    if not parsed:
        await message.reply_text(
            "❌ <b>Invalid subtitle caption.</b>\n\n"
            "<b>Format:</b> <code>AniList ID | Episode | Language Label | lang_code</code>\n"
            "<b>Example:</b> <code>21355 | 1 | English | en</code>",
            parse_mode=ParseMode.HTML, quote=True
        )
        return

    anilist_id = parsed["anilist_id"]
    episode    = parsed["episode"]
    label      = parsed["label"]
    lang       = parsed["lang"]

    status_msg = await message.reply_text(
        "🔍 <b>Looking up anime…</b>", parse_mode=ParseMode.HTML, quote=True
    )

    # ── Look up anime in DB ──────────────────────────────────────────────────
    anime_meta = await site_db.get_anime_by_anilist_id(anilist_id)
    if not anime_meta:
        await status_msg.edit_text(
            f"❌ <b>AniList ID {anilist_id} not found in the DB.</b>\n"
            "Upload the video first, then attach the subtitle.",
            parse_mode=ParseMode.HTML
        )
        return

    anime_id   = anime_meta["id"]
    anime_name = anime_meta["name"]
    slug       = anime_meta["slug"]

    # ── Store soft subtitle in DB ────────────────────────────────────────────
    file_id = message.document.file_id
    sub_id = await site_db.upsert_subtitle(
        anime_id=anime_id, episode=episode, label=label, lang=lang, file_id=file_id
    )

    base = Server.URL.rstrip("/")

    await status_msg.edit_text(
        "✅ <b>Soft subtitle saved.</b>\n\n"
        f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
        f"<b>Episode:</b> {episode} | <b>Lang:</b> {label} ({lang})\n\n"
        "🎞️ <b>Creating hard-subbed + soft-subbed videos…</b> (this takes a while)",
        parse_mode=ParseMode.HTML
    )

    # ── Find a source episode ─────────────────────────────────────────────────
    # Prefer the raw/sub source; skip any existing hsub as source
    all_eps = await site_db.get_episode_qualities(anilist_id, episode)
    source_ep = next(
        (e for e in all_eps if e["audio_type"] == "sub"), None
    ) or next(
        (e for e in all_eps if e["audio_type"] not in ("hsub",)), None
    )

    if not source_ep or not source_ep.get("dump_msg_id") or not Telegram.DUMP_CHANNEL:
        await status_msg.edit_text(
            "✅ <b>Soft subtitle saved.</b>\n\n"
            f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
            f"<b>Episode:</b> {episode} | <b>Lang:</b> {label} ({lang})\n\n"
            "⚠️ <b>No source video found</b> — hard/soft-sub skipped.\n"
            "Upload the video first, then re-attach the subtitle.",
            parse_mode=ParseMode.HTML
        )
        return

    # ── Download video + subtitle, encode both variants ──────────────────────
    loop = asyncio.get_event_loop()
    quality         = source_ep["quality"]
    dump_channel_id = source_ep.get("dump_channel_id") or Telegram.DUMP_CHANNEL

    hs_dump_msg_id = None
    hs_file_size   = 0
    ss_dump_msg_id = None
    ss_file_size   = 0

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Get video file_id from dump channel message
            try:
                src_msg = await bot.get_messages(dump_channel_id, source_ep["dump_msg_id"])
                video_media = getattr(src_msg, "video", None) or getattr(src_msg, "document", None)
                if not video_media:
                    raise RuntimeError("Dump channel message has no video/document")
                video_file_id = video_media.file_id
            except Exception as e:
                raise RuntimeError(f"Could not fetch dump channel message: {e}")

            # Download video
            await status_msg.edit_text(
                "⬇️ <b>Downloading source video…</b>", parse_mode=ParseMode.HTML
            )
            video_path = os.path.join(tmpdir, "source.mp4")
            dl = await bot.download_media(video_file_id, file_name=video_path)
            if not dl or not os.path.exists(dl):
                raise RuntimeError("Video download returned no file")

            # Download subtitle
            fname = message.document.file_name or f"sub.{lang}.vtt"
            sub_dl_path = os.path.join(tmpdir, fname)
            sub_dl = await bot.download_media(file_id, file_name=sub_dl_path)
            if not sub_dl or not os.path.exists(sub_dl):
                raise RuntimeError("Subtitle download returned no file")

            # ── Hard-sub encode ──────────────────────────────────────────────
            await status_msg.edit_text(
                "🔥 <b>[1/4] Burning subtitles into video (HSUB)…</b>",
                parse_mode=ParseMode.HTML
            )
            hs_path = os.path.join(tmpdir, "hardsub.mp4")
            hs_ok = await loop.run_in_executor(None, _run_hardsub_ffmpeg, dl, sub_dl, hs_path)
            if not hs_ok:
                raise RuntimeError("Hard-sub ffmpeg encode failed")

            # ── Soft-sub encode ──────────────────────────────────────────────
            await status_msg.edit_text(
                "💬 <b>[2/4] Muxing subtitle track (SUB)…</b>",
                parse_mode=ParseMode.HTML
            )
            ss_path = os.path.join(tmpdir, "softsub.mp4")
            ss_ok = await loop.run_in_executor(None, _run_softsub_ffmpeg, dl, sub_dl, ss_path, lang)
            if not ss_ok:
                logger.warning("Soft-sub encode failed — will skip SUB upload")

            # ── Upload hard-sub ──────────────────────────────────────────────
            await status_msg.edit_text(
                "⬆️ <b>[3/4] Uploading hard-subbed video (HSUB)…</b>",
                parse_mode=ParseMode.HTML
            )
            hs_caption = "#{} | E{:02d} | HSUB | {}\n<b>{}</b> (AniList: {})".format(
                _safe_hashtag(slug), episode, quality, anime_name, anilist_id
            )
            hs_sent = await _send_with_flood_retry(
                bot=bot,
                chat_id=Telegram.DUMP_CHANNEL,
                video=hs_path,
                caption=hs_caption,
                status_msg=status_msg,
            )
            if hs_sent is None:
                return
            hs_dump_msg_id = hs_sent.id
            hs_file_size   = os.path.getsize(hs_path)

            # ── Upload soft-sub ──────────────────────────────────────────────
            if ss_ok and os.path.exists(ss_path):
                await status_msg.edit_text(
                    "⬆️ <b>[4/4] Uploading soft-subbed video (SUB)…</b>",
                    parse_mode=ParseMode.HTML
                )
                ss_caption = "#{} | E{:02d} | SUB | {}\n<b>{}</b> (AniList: {})".format(
                    _safe_hashtag(slug), episode, quality, anime_name, anilist_id
                )
                ss_sent = await _send_with_flood_retry(
                    bot=bot,
                    chat_id=Telegram.DUMP_CHANNEL,
                    video=ss_path,
                    caption=ss_caption,
                    status_msg=status_msg,
                )
                if ss_sent is not None:
                    ss_dump_msg_id = ss_sent.id
                    ss_file_size   = os.path.getsize(ss_path)

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text(
                "🛑 <b>Sub processing cancelled by /stop.</b>", parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return
    except Exception as e:
        logger.error("Subtitle pipeline error: %s", e, exc_info=True)
        await status_msg.edit_text(
            "⚠️ <b>Soft subtitle saved, but video encoding failed.</b>\n"
            f"<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # ── Save HSUB episode to DB ───────────────────────────────────────────────
    hs_token = None
    if hs_dump_msg_id:
        hs_token = await site_db.upsert_episode(
            anime_id=anime_id,
            episode=episode,
            audio_type="hsub",
            quality=quality,
            dump_msg_id=hs_dump_msg_id,
            dump_channel_id=Telegram.DUMP_CHANNEL,
            file_size=hs_file_size,
            anilist_id=anilist_id,
        )

    # ── Save SUB episode to DB ────────────────────────────────────────────────
    ss_token = None
    if ss_dump_msg_id:
        ss_token = await site_db.upsert_episode(
            anime_id=anime_id,
            episode=episode,
            audio_type="sub",
            quality=quality,
            dump_msg_id=ss_dump_msg_id,
            dump_channel_id=Telegram.DUMP_CHANNEL,
            file_size=ss_file_size,
            anilist_id=anilist_id,
        )

    # ── Build result message ──────────────────────────────────────────────────
    lines = [
        "✅ <b>Done!</b>\n",
        f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>",
        f"<b>Episode:</b> {episode} | <b>Sub:</b> {label} ({lang})\n",
        "📌 <b>Soft sub</b> → attached to all episode variants for this ep",
    ]
    if hs_token:
        lines.append(
            "🔥 <b>HSUB</b> (burned-in) token:\n"
            f"<code>{hs_token}</code>\n"
            f"Player: <code>{base}/player/{hs_token}</code>"
        )
    if ss_token:
        lines.append(
            "💬 <b>SUB</b> (soft-muxed) token:\n"
            f"<code>{ss_token}</code>\n"
            f"Player: <code>{base}/player/{ss_token}</code>"
        )

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
