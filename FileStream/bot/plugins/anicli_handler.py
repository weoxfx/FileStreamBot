"""
ani-cli integration — /fetch command.

Fetches an episode from an online source via ani-cli (ani-cli/ani-cli script),
processes it (watermark), uploads to Telegram dump channel, and registers in DB.

Nothing is stored permanently on disk — all downloads go into a TemporaryDirectory
that is wiped automatically on exit.

Usage:
  /fetch <anilist_id> <episode> [quality]

Example:
  /fetch 21355 1 1080p

The quality argument is only for labelling — ani-cli picks the best available stream.
Omit it to default to "1080p".
"""
import os
import re
import asyncio
import logging
import shutil
import tempfile
import subprocess

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from FileStream.bot import FileStream
from FileStream.config import Telegram, Server
from FileStream.utils.watermark import _run_ffmpeg
from FileStream.utils import bot_db, site_db
from FileStream.utils.anilist import fetch_anime_by_id

logger = logging.getLogger(__name__)

_ANICLI_PATH = os.path.abspath(os.path.join("ani-cli", "ani-cli"))

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}

_MAX_FLOOD_RETRIES = 3


def _safe_hashtag(slug: str) -> str:
    return re.sub(r"[^\w]", "_", slug, flags=re.ASCII)


def _find_downloaded_file(directory: str) -> str | None:
    """Return the first video file found in directory, or None."""
    for fname in os.listdir(directory):
        _, ext = os.path.splitext(fname.lower())
        if ext in _VIDEO_EXTS:
            fpath = os.path.join(directory, fname)
            if os.path.getsize(fpath) > 0:
                return fpath
    return None


async def _flood_retry_send(bot, chat_id, video, caption, status_msg):
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
                try:
                    await status_msg.edit_text(
                        f"❌ <b>Rate limited ({fw.value}s). Upload aborted.</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return None
            await asyncio.sleep(fw.value)
        except Exception:
            raise


@FileStream.on_message(
    filters.private
    & filters.command("fetch")
    & filters.user([Telegram.OWNER_ID] + list(Telegram.AUTH_USERS)),
)
async def fetch_handler(bot: Client, message: Message):
    args = message.command[1:]

    if len(args) < 2:
        await message.reply_text(
            "❌ <b>Usage:</b> <code>/fetch &lt;anilist_id&gt; &lt;episode&gt; [quality]</code>\n\n"
            "<b>Example:</b> <code>/fetch 21355 1 1080p</code>\n\n"
            "ani-cli will fetch the episode from online sources.\n"
            "Nothing is saved on disk — only uploaded to Telegram.",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    try:
        anilist_id = int(args[0])
        episode    = int(args[1])
    except ValueError:
        await message.reply_text(
            "❌ <b>AniList ID and episode must be numbers.</b>",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    quality = args[2] if len(args) >= 3 else "1080p"

    if not Telegram.DUMP_CHANNEL:
        await message.reply_text("❌ DUMP_CHANNEL is not configured.", quote=True)
        return

    if not os.path.exists(_ANICLI_PATH):
        await message.reply_text(
            "❌ <b>ani-cli script not found.</b>\n"
            f"Expected at: <code>{_ANICLI_PATH}</code>",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    status_msg = await message.reply_text(
        f"🔍 <b>Looking up AniList ID {anilist_id}…</b>",
        parse_mode=ParseMode.HTML,
        quote=True,
    )

    anime_info = await fetch_anime_by_id(anilist_id)
    if not anime_info:
        await status_msg.edit_text(
            f"❌ <b>AniList ID {anilist_id} not found.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    anime_name     = anime_info["name"]
    slug           = anime_info["slug"]
    mal_id         = anime_info.get("mal_id")
    cover_url      = anime_info.get("cover_url") or ""
    synopsis       = anime_info.get("synopsis") or ""
    total_episodes = anime_info.get("total_episodes")

    existing = await site_db.get_episode_qualities(anilist_id, episode)
    dup = next(
        (q for q in existing if q["audio_type"] == "sub" and q["quality"] == quality),
        None,
    )
    if dup:
        base = Server.URL.rstrip("/")
        await status_msg.edit_text(
            "⚠️ <b>Already exists — skipping duplicate.</b>\n\n"
            f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
            f"<b>Episode:</b> {episode} | <b>Type:</b> SUB | <b>Quality:</b> {quality}\n\n"
            f"<b>Player:</b>\n<code>{base}/player/{dup['stream_token']}</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await status_msg.edit_text(
        f"🌐 <b>Fetching episode {episode} via ani-cli…</b>\n\n"
        f"<b>Anime:</b> {anime_name}\n"
        "<i>This may take several minutes depending on source speed.</i>",
        parse_mode=ParseMode.HTML,
    )

    loop = asyncio.get_event_loop()
    dump_msg_id = None

    try:
        with tempfile.TemporaryDirectory(prefix="tsuki_fetch_") as tmpdir:
            # ── Run ani-cli in tmpdir to download the episode ─────────────────
            env = os.environ.copy()
            env["HOME"] = tmpdir  # isolate ani-cli history/state

            # Ensure all required tools are on PATH — the workflow process may
            # have a leaner PATH than the interactive shell, missing Nix store
            # bin dirs where curl/ffmpeg/etc. actually live.
            _extra_dirs = set()
            for _tool in ("curl", "ffmpeg", "fzf", "mpv", "aria2c", "wget"):
                _p = shutil.which(_tool)
                if _p:
                    _extra_dirs.add(os.path.dirname(_p))
            if _extra_dirs:
                env["PATH"] = ":".join(_extra_dirs) + ":" + env.get("PATH", "")

            cmd = [
                "bash", _ANICLI_PATH,
                "-d",
                "-e", str(episode),
                anime_name,
            ]

            try:
                proc = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: subprocess.run(
                            cmd,
                            cwd=tmpdir,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=1800,
                        ),
                    ),
                    timeout=1860,
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text(
                    "❌ <b>ani-cli download timed out (30 min).</b>\n"
                    "The source may be slow or unavailable.",
                    parse_mode=ParseMode.HTML,
                )
                return

            if proc.returncode != 0:
                stderr_tail = proc.stderr.decode(errors="replace")[-800:]
                await status_msg.edit_text(
                    "❌ <b>ani-cli download failed.</b>\n\n"
                    f"<code>{stderr_tail}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            dl_path = _find_downloaded_file(tmpdir)
            if not dl_path:
                await status_msg.edit_text(
                    "❌ <b>ani-cli ran but no video file was found.</b>\n"
                    "The episode may not be available from the current source.",
                    parse_mode=ParseMode.HTML,
                )
                return

            file_size = os.path.getsize(dl_path)

            # ── Watermark ─────────────────────────────────────────────────────
            await status_msg.edit_text(
                "🎨 <b>Applying Tsukuyomi watermark…</b>\n\n"
                f"<b>{anime_name}</b> E{episode:02d} [SUB] [{quality}]",
                parse_mode=ParseMode.HTML,
            )
            wm_path = os.path.join(tmpdir, "wm_output.mp4")
            ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
            upload_path = wm_path if (ok and os.path.exists(wm_path)) else dl_path
            file_size = os.path.getsize(upload_path)

            # ── Upload to dump channel ────────────────────────────────────────
            await status_msg.edit_text(
                "⬆️ <b>Uploading to dump channel…</b>\n\n"
                f"<b>{anime_name}</b> E{episode:02d} [SUB] [{quality}]",
                parse_mode=ParseMode.HTML,
            )
            dump_caption = "#{} | E{:02d} | SUB | {}\n<b>{}</b> (AniList: {}) [ani-cli]".format(
                _safe_hashtag(slug), episode, quality, anime_name, anilist_id
            )
            sent = await _flood_retry_send(
                bot=bot,
                chat_id=Telegram.DUMP_CHANNEL,
                video=upload_path,
                caption=dump_caption,
                status_msg=status_msg,
            )
            if sent is None:
                return
            dump_msg_id = sent.id

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text(
                "🛑 <b>Fetch cancelled.</b>", parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return
    except Exception as e:
        logger.error("ani-cli fetch error: %s", e, exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ <b>Fetch failed:</b> <code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if not dump_msg_id:
        return

    # ── Save to DB ────────────────────────────────────────────────────────────
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
        audio_type="sub",
        quality=quality,
        dump_msg_id=dump_msg_id,
        dump_channel_id=Telegram.DUMP_CHANNEL,
        file_size=file_size,
        anilist_id=anilist_id,
    )

    base       = Server.URL.rstrip("/")
    player_url = f"{base}/player/{stream_token}"

    if Telegram.ULOG_CHANNEL:
        try:
            await bot.send_message(
                Telegram.ULOG_CHANNEL,
                "✅ <b>#AniCliFetch</b>\n"
                f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
                f"<b>Episode:</b> {episode} | <b>Type:</b> SUB | <b>Quality:</b> {quality}\n"
                f"<b>Token:</b> <code>{stream_token}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    genres_txt = ", ".join(anime_info.get("genres", [])[:3])
    await status_msg.edit_text(
        "✅ <b>Done! (ani-cli fetch)</b>\n\n"
        f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
        + (f"<b>Genres:</b> {genres_txt}\n" if genres_txt else "")
        + f"<b>Episode:</b> {episode} | <b>Type:</b> SUB | <b>Quality:</b> {quality}\n\n"
        f"<b>Token:</b>\n<code>{stream_token}</code>\n\n"
        f"<b>Player:</b>\n<code>{player_url}</code>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
