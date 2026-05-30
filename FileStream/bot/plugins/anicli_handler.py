"""
ani-cli integration — /fetch and /batch commands.

/fetch <anilist_id> <episode> [quality]
    Fetch a single episode via ani-cli, watermark it, upload to Telegram.

/batch <anilist_id> <start_ep> <end_ep> [quality]
    Fetch a range of episodes (max 50) sequentially.

Nothing is stored permanently on disk — all downloads use TemporaryDirectory.
"""
import os
import re
import asyncio
import logging
import tempfile
import subprocess

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from FileStream.bot import FileStream
from FileStream.config import Telegram, Server
from FileStream.utils.watermark import _run_ffmpeg
from FileStream.utils import site_db
from FileStream.utils.anilist import fetch_anime_by_id

logger = logging.getLogger(__name__)

_ANICLI_PATH = os.path.abspath(os.path.join("ani-cli", "ani-cli"))
_VIDEO_EXTS  = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}
_MAX_FLOOD_RETRIES = 3

# Capture the full login-shell PATH once at import time.
# The workflow process may start with a stripped-down PATH that is missing the
# Nix store bin directory where curl, ffmpeg, etc. actually live.
# Running bash -lc sources all profile files and gives us the complete PATH.
try:
    _r = subprocess.run(
        ["bash", "-lc", "echo $PATH"],
        capture_output=True, text=True, timeout=8,
    )
    _LOGIN_SHELL_PATH = _r.stdout.strip() if _r.returncode == 0 else ""
except Exception:
    _LOGIN_SHELL_PATH = ""


def _safe_hashtag(slug: str) -> str:
    return re.sub(r"[^\w]", "_", slug, flags=re.ASCII)


def _find_downloaded_file(directory: str) -> str | None:
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


async def _do_one_fetch(
    bot,
    anilist_id: int,
    episode: int,
    quality: str,
    anime_name: str,
    slug: str,
    mal_id,
    cover_url: str,
    synopsis: str,
    total_episodes,
    status_msg,
) -> str | None:
    """
    Download one episode via ani-cli, watermark it, upload to dump channel,
    save to DB. Returns the stream_token on success, None on failure.
    Updates status_msg with progress throughout.
    """
    loop = asyncio.get_event_loop()
    dump_msg_id = None
    file_size   = 0

    try:
        with tempfile.TemporaryDirectory(prefix="tsuki_fetch_") as tmpdir:
            env = os.environ.copy()
            env["HOME"] = tmpdir
            # Use the full login-shell PATH so curl/ffmpeg are always found.
            if _LOGIN_SHELL_PATH:
                env["PATH"] = _LOGIN_SHELL_PATH

            cmd = ["bash", _ANICLI_PATH, "-d", "-e", str(episode), anime_name]

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
                    f"❌ <b>E{episode:02d}: ani-cli timed out (30 min).</b>",
                    parse_mode=ParseMode.HTML,
                )
                return None

            if proc.returncode != 0:
                stderr_tail = proc.stderr.decode(errors="replace")[-800:]
                await status_msg.edit_text(
                    f"❌ <b>ani-cli download failed for E{episode:02d}.</b>\n\n"
                    f"<code>{stderr_tail}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return None

            dl_path = _find_downloaded_file(tmpdir)
            if not dl_path:
                await status_msg.edit_text(
                    f"❌ <b>E{episode:02d}: ani-cli ran but no video file was found.</b>",
                    parse_mode=ParseMode.HTML,
                )
                return None

            await status_msg.edit_text(
                f"🎨 <b>Applying watermark — E{episode:02d}…</b>\n"
                f"<b>{anime_name}</b> [{quality}]",
                parse_mode=ParseMode.HTML,
            )
            wm_path = os.path.join(tmpdir, "wm_output.mp4")
            ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
            upload_path = wm_path if (ok and os.path.exists(wm_path)) else dl_path
            file_size   = os.path.getsize(upload_path)

            await status_msg.edit_text(
                f"⬆️ <b>Uploading E{episode:02d} to dump channel…</b>\n"
                f"<b>{anime_name}</b> [{quality}]",
                parse_mode=ParseMode.HTML,
            )
            dump_caption = (
                "#{} | E{:02d} | SUB | {}\n"
                "<b>{}</b> (AniList: {}) [ani-cli]"
            ).format(_safe_hashtag(slug), episode, quality, anime_name, anilist_id)

            sent = await _flood_retry_send(
                bot=bot,
                chat_id=Telegram.DUMP_CHANNEL,
                video=upload_path,
                caption=dump_caption,
                status_msg=status_msg,
            )
            if sent is None:
                return None
            dump_msg_id = sent.id

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text("🛑 <b>Fetch cancelled.</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return None
    except Exception as e:
        logger.error("ani-cli fetch error E%s: %s", episode, e, exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ <b>E{episode:02d} failed:</b> <code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return None

    if not dump_msg_id:
        return None

    anime_id = await site_db.get_or_create_anime(
        anime_name, slug, anilist_id,
        mal_id=mal_id,
        cover_url=cover_url,
        synopsis=synopsis,
        total_episodes=total_episodes,
    )
    return await site_db.upsert_episode(
        anime_id=anime_id,
        episode=episode,
        audio_type="sub",
        quality=quality,
        dump_msg_id=dump_msg_id,
        dump_channel_id=Telegram.DUMP_CHANNEL,
        file_size=file_size,
        anilist_id=anilist_id,
    )


# ── /fetch ────────────────────────────────────────────────────────────────────

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
            "ani-cli fetches the episode from online sources.\n"
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

    stream_token = await _do_one_fetch(
        bot, anilist_id, episode, quality,
        anime_name, slug, mal_id, cover_url, synopsis, total_episodes,
        status_msg,
    )
    if not stream_token:
        return

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


# ── /batch ────────────────────────────────────────────────────────────────────

@FileStream.on_message(
    filters.private
    & filters.command("batch")
    & filters.user([Telegram.OWNER_ID] + list(Telegram.AUTH_USERS)),
)
async def batch_handler(bot: Client, message: Message):
    args = message.command[1:]

    if len(args) < 3:
        await message.reply_text(
            "❌ <b>Usage:</b>\n"
            "<code>/batch &lt;anilist_id&gt; &lt;start_ep&gt; &lt;end_ep&gt; [quality]</code>\n\n"
            "<b>Example:</b> <code>/batch 21355 1 12 1080p</code>\n\n"
            "Fetches episodes sequentially via ani-cli (max 50 at a time).",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    try:
        anilist_id = int(args[0])
        start_ep   = int(args[1])
        end_ep     = int(args[2])
    except ValueError:
        await message.reply_text(
            "❌ <b>AniList ID and episode numbers must be integers.</b>",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    if start_ep < 1 or start_ep > end_ep:
        await message.reply_text("❌ <b>start_ep must be ≥ 1 and ≤ end_ep.</b>", parse_mode=ParseMode.HTML, quote=True)
        return
    if end_ep - start_ep >= 50:
        await message.reply_text("❌ <b>Max 50 episodes per batch.</b>", parse_mode=ParseMode.HTML, quote=True)
        return

    quality = args[3] if len(args) >= 4 else "1080p"
    total   = end_ep - start_ep + 1

    if not Telegram.DUMP_CHANNEL:
        await message.reply_text("❌ DUMP_CHANNEL is not configured.", quote=True)
        return

    if not os.path.exists(_ANICLI_PATH):
        await message.reply_text(
            f"❌ <b>ani-cli script not found.</b>\nExpected at: <code>{_ANICLI_PATH}</code>",
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
    base           = Server.URL.rstrip("/")

    done_tokens: list[tuple[int, str]] = []
    skipped:     list[int]             = []
    failed:      list[int]             = []

    def _status_line():
        return (
            f"<b>Done:</b> {len(done_tokens)} | "
            f"<b>Skipped:</b> {len(skipped)} | "
            f"<b>Failed:</b> {len(failed)}"
        )

    for i, episode in enumerate(range(start_ep, end_ep + 1), 1):
        existing = await site_db.get_episode_qualities(anilist_id, episode)
        dup = next(
            (q for q in existing if q["audio_type"] == "sub" and q["quality"] == quality),
            None,
        )
        if dup:
            skipped.append(episode)
            try:
                await status_msg.edit_text(
                    f"⏩ <b>Batch {i}/{total} — E{episode:02d} already exists, skipping…</b>\n\n"
                    f"<b>Anime:</b> {anime_name} [{quality}]\n{_status_line()}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            continue

        try:
            await status_msg.edit_text(
                f"🌐 <b>Batch {i}/{total} — Fetching E{episode:02d}…</b>\n\n"
                f"<b>Anime:</b> {anime_name} [{quality}]\n{_status_line()}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        stream_token = await _do_one_fetch(
            bot, anilist_id, episode, quality,
            anime_name, slug, mal_id, cover_url, synopsis, total_episodes,
            status_msg,
        )

        if stream_token:
            done_tokens.append((episode, stream_token))
        else:
            failed.append(episode)

    lines = [
        f"✅ <b>Batch complete — {anime_name}</b>",
        f"<b>Episodes:</b> E{start_ep:02d}–E{end_ep:02d} | <b>Quality:</b> {quality}",
        _status_line(),
        "",
    ]
    if done_tokens:
        lines.append("<b>Player links:</b>")
        for ep_num, tok in done_tokens[:8]:
            lines.append(f"• E{ep_num:02d}: <code>{base}/player/{tok}</code>")
        if len(done_tokens) > 8:
            lines.append(f"  … and {len(done_tokens) - 8} more")
    if failed:
        lines.append(f"\n⚠️ <b>Failed:</b> {', '.join(f'E{e:02d}' for e in failed)}")
    if skipped:
        lines.append(f"⏩ <b>Already existed:</b> {', '.join(f'E{e:02d}' for e in skipped)}")

    try:
        await status_msg.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        pass
