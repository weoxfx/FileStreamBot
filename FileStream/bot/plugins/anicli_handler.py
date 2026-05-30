"""
ani-cli integration — /fetch and /batch commands.

/fetch <anilist_id> <episode> [quality]
    Fetch a single episode via ani-cli — automatically generates both
    SUB and DUB versions in one go (DUB silently skipped if unavailable).

/batch <anilist_id> <start_ep> <end_ep> [quality]
    Fetch a range of episodes (max 50) sequentially, sub+dub each.

Nothing is stored permanently on disk — all downloads use TemporaryDirectory.

Note: AllAnime only serves hardsub (burned-in subtitles) MP4 files.
There is no true softsub option from this source.
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
from FileStream.utils.allanime import search_show, get_episodes_list

logger = logging.getLogger(__name__)

_ANICLI_PATH = os.path.abspath(os.path.join("ani-cli", "ani-cli"))
_VIDEO_EXTS  = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}
_MAX_FLOOD_RETRIES = 3


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
    allanime_id: str = "",
    audio_type: str = "sub",
) -> str | None:
    """
    Download one episode via ani-cli, watermark it, upload to dump channel,
    save to DB. Returns the stream_token on success, None on failure.

    audio_type: "sub" (Japanese audio + hardsub) or "dub" (English dub)
    AllAnime only provides hardsub content — there is no real softsub option.
    """
    loop = asyncio.get_event_loop()
    dump_msg_id = None
    file_size   = 0

    ani_mode = "dub" if audio_type == "dub" else "sub"

    try:
        with tempfile.TemporaryDirectory(prefix="tsuki_fetch_") as tmpdir:
            env = os.environ.copy()
            env["HOME"] = tmpdir
            env["ANI_CLI_DOWNLOAD_DIR"] = tmpdir
            env["ANI_CLI_QUALITY"] = quality
            env["ANI_CLI_MODE"] = ani_mode
            env["ANI_CLI_SUB_TYPE"] = ani_mode
            if allanime_id:
                env["ANI_CLI_SHOW_ID"] = allanime_id

            if audio_type == "dub":
                cmd = ["bash", _ANICLI_PATH, "-d", "--dub", "-e", str(episode), anime_name]
            else:
                cmd = ["bash", _ANICLI_PATH, "-d", "-e", str(episode), anime_name]
            logger.info(
                "ani-cli cmd: %s (show_id=%s, audio_type=%s)",
                " ".join(cmd), allanime_id, audio_type,
            )

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
                    f"❌ <b>E{episode:02d} [{audio_type.upper()}]: ani-cli timed out (30 min).</b>",
                    parse_mode=ParseMode.HTML,
                )
                return None

            stderr_out = proc.stderr.decode(errors="replace")
            stdout_out = proc.stdout.decode(errors="replace")
            logger.info("ani-cli stdout: %s", stdout_out[-500:])
            if stderr_out:
                logger.info("ani-cli stderr: %s", stderr_out[-500:])

            if proc.returncode != 0:
                await status_msg.edit_text(
                    f"❌ <b>ani-cli failed — E{episode:02d} [{audio_type.upper()}].</b>\n\n"
                    f"<code>{stderr_out[-600:]}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return None

            dl_path = _find_downloaded_file(tmpdir)
            if not dl_path:
                await status_msg.edit_text(
                    f"❌ <b>E{episode:02d} [{audio_type.upper()}]: no video file found after download.</b>\n\n"
                    f"<code>{stdout_out[-300:]}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return None

            await status_msg.edit_text(
                f"🎨 <b>Watermarking E{episode:02d} [{audio_type.upper()}]…</b>\n"
                f"<b>{anime_name}</b> [{quality}]",
                parse_mode=ParseMode.HTML,
            )
            wm_path = os.path.join(tmpdir, "wm_output.mp4")
            ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
            upload_path = wm_path if (ok and os.path.exists(wm_path)) else dl_path
            file_size   = os.path.getsize(upload_path)

            await status_msg.edit_text(
                f"⬆️ <b>Uploading E{episode:02d} [{audio_type.upper()}] to dump channel…</b>\n"
                f"<b>{anime_name}</b> [{quality}]",
                parse_mode=ParseMode.HTML,
            )
            dump_caption = (
                "#{} | E{:02d} | {} | {}\n"
                "<b>{}</b> (AniList: {}) [ani-cli]"
            ).format(
                _safe_hashtag(slug), episode,
                audio_type.upper(), quality,
                anime_name, anilist_id,
            )

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
        logger.error("ani-cli fetch error E%s [%s]: %s", episode, audio_type, e, exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ <b>E{episode:02d} [{audio_type.upper()}] failed:</b> <code>{e}</code>",
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
        audio_type=audio_type,
        quality=quality,
        dump_msg_id=dump_msg_id,
        dump_channel_id=Telegram.DUMP_CHANNEL,
        file_size=file_size,
        anilist_id=anilist_id,
    )


async def _resolve_ids(anime_name: str, total_episodes) -> tuple[str, str]:
    """
    Resolve AllAnime show IDs for both sub and dub in parallel.
    Returns (sub_id, dub_id) — either may be empty string if not found.
    """
    sub_task = search_show(anime_name, mode="sub", expected_episodes=total_episodes)
    dub_task = search_show(anime_name, mode="dub", expected_episodes=total_episodes)
    sub_results, dub_results = await asyncio.gather(sub_task, dub_task)

    sub_id = sub_results[0]["id"] if sub_results else ""
    dub_id = dub_results[0]["id"] if dub_results else ""

    logger.info(
        "AllAnime IDs — %s: sub=%s dub=%s",
        anime_name, sub_id or "not found", dub_id or "not found",
    )
    return sub_id, dub_id


# ── /fetch ─────────────────────────────────────────────────────────────────────

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
            "Automatically fetches <b>both SUB and DUB</b> in one go.\n"
            "DUB is silently skipped if not available on AllAnime.\n\n"
            "<b>quality:</b> <code>best</code> | <code>1080p</code> (default) | <code>720p</code> | <code>480p</code>\n\n"
            "<b>Examples:</b>\n"
            "<code>/fetch 21 1</code>\n"
            "<code>/fetch 21 1 720p</code>",
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
    base           = Server.URL.rstrip("/")

    # Resolve AllAnime IDs for sub and dub in parallel
    await status_msg.edit_text(
        f"🔎 <b>Resolving sources for {anime_name}…</b>",
        parse_mode=ParseMode.HTML,
    )
    sub_id, dub_id = await _resolve_ids(anime_name, total_episodes)

    results = {}  # audio_type → stream_token

    # ── SUB ──
    existing_sub = await site_db.get_episode_qualities(anilist_id, episode)
    dup_sub = next(
        (q for q in existing_sub if q["audio_type"] == "sub" and q["quality"] == quality),
        None,
    )
    if dup_sub:
        logger.info("E%02d SUB already exists — skipping", episode)
        results["sub"] = dup_sub["stream_token"]
    else:
        await status_msg.edit_text(
            f"🌐 <b>Fetching E{episode:02d} [SUB] via ani-cli…</b>\n\n"
            f"<b>Anime:</b> {anime_name}\n"
            "<i>This may take several minutes.</i>",
            parse_mode=ParseMode.HTML,
        )
        tok = await _do_one_fetch(
            bot, anilist_id, episode, quality,
            anime_name, slug, mal_id, cover_url, synopsis, total_episodes,
            status_msg, allanime_id=sub_id, audio_type="sub",
        )
        if tok:
            results["sub"] = tok

    # ── DUB ──
    if dub_id:
        existing_dub = await site_db.get_episode_qualities(anilist_id, episode)
        dup_dub = next(
            (q for q in existing_dub if q["audio_type"] == "dub" and q["quality"] == quality),
            None,
        )
        if dup_dub:
            logger.info("E%02d DUB already exists — skipping", episode)
            results["dub"] = dup_dub["stream_token"]
        else:
            await status_msg.edit_text(
                f"🌐 <b>Fetching E{episode:02d} [DUB] via ani-cli…</b>\n\n"
                f"<b>Anime:</b> {anime_name}\n"
                "<i>This may take several minutes.</i>",
                parse_mode=ParseMode.HTML,
            )
            try:
                tok = await _do_one_fetch(
                    bot, anilist_id, episode, quality,
                    anime_name, slug, mal_id, cover_url, synopsis, total_episodes,
                    status_msg, allanime_id=dub_id, audio_type="dub",
                )
                if tok:
                    results["dub"] = tok
            except Exception as e:
                logger.warning("DUB fetch failed for E%02d — skipping: %s", episode, e)
    else:
        logger.info("No DUB found on AllAnime for %r — skipping dub", anime_name)

    if not results:
        return

    if Telegram.ULOG_CHANNEL:
        try:
            types_done = " + ".join(t.upper() for t in results)
            await bot.send_message(
                Telegram.ULOG_CHANNEL,
                "✅ <b>#AniCliFetch</b>\n"
                f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>\n"
                f"<b>Episode:</b> {episode} | <b>Types:</b> {types_done} | <b>Quality:</b> {quality}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    genres_txt = ", ".join(anime_info.get("genres", [])[:3])
    lines = [
        "✅ <b>Done! (ani-cli fetch)</b>",
        "",
        f"<b>Anime:</b> {anime_name} <code>({anilist_id})</code>",
    ]
    if genres_txt:
        lines.append(f"<b>Genres:</b> {genres_txt}")
    lines.append(f"<b>Episode:</b> {episode} | <b>Quality:</b> {quality}")
    lines.append("")
    for atype, tok in results.items():
        lines.append(f"<b>{atype.upper()} Player:</b>")
        lines.append(f"<code>{base}/player/{tok}</code>")
    if "dub" not in results and not dub_id:
        lines.append("\n<i>ℹ️ DUB not available on AllAnime for this title.</i>")

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ── /batch ─────────────────────────────────────────────────────────────────────

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
            "Fetches each episode as <b>SUB + DUB</b> (DUB skipped if unavailable).\n\n"
            "<b>quality:</b> <code>best</code> | <code>1080p</code> (default) | <code>720p</code>\n\n"
            "<b>Examples:</b>\n"
            "<code>/batch 21 1 12</code>\n"
            "<code>/batch 21 1 12 720p</code>\n\n"
            "Max 50 episodes per batch.",
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
        await message.reply_text(
            "❌ <b>start_ep must be ≥ 1 and ≤ end_ep.</b>",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return
    if end_ep - start_ep >= 50:
        await message.reply_text(
            "❌ <b>Max 50 episodes per batch.</b>",
            parse_mode=ParseMode.HTML,
            quote=True,
        )
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

    # Resolve AllAnime IDs for sub and dub once for the whole batch
    await status_msg.edit_text(
        f"🔎 <b>Resolving sources for {anime_name}…</b>",
        parse_mode=ParseMode.HTML,
    )
    sub_id, dub_id = await _resolve_ids(anime_name, total_episodes)

    done_sub:  list[tuple[int, str]] = []
    done_dub:  list[tuple[int, str]] = []
    skipped:   list[int]             = []
    failed:    list[int]             = []

    def _status_line() -> str:
        return (
            f"<b>Done:</b> {len(done_sub)}S/{len(done_dub)}D | "
            f"<b>Skipped:</b> {len(skipped)} | "
            f"<b>Failed:</b> {len(failed)}"
        )

    for i, episode in enumerate(range(start_ep, end_ep + 1), 1):
        existing = await site_db.get_episode_qualities(anilist_id, episode)
        has_sub  = any(q["audio_type"] == "sub"  and q["quality"] == quality for q in existing)
        has_dub  = any(q["audio_type"] == "dub"  and q["quality"] == quality for q in existing)
        need_sub = not has_sub
        need_dub = bool(dub_id) and not has_dub

        if not need_sub and not need_dub:
            skipped.append(episode)
            try:
                await status_msg.edit_text(
                    f"⏩ <b>Batch {i}/{total} — E{episode:02d} already complete, skipping…</b>\n\n"
                    f"<b>Anime:</b> {anime_name} [{quality}]\n{_status_line()}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            continue

        ep_ok = False

        # SUB
        if need_sub:
            try:
                await status_msg.edit_text(
                    f"🌐 <b>Batch {i}/{total} — E{episode:02d} [SUB]…</b>\n\n"
                    f"<b>Anime:</b> {anime_name} [{quality}]\n{_status_line()}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            tok = await _do_one_fetch(
                bot, anilist_id, episode, quality,
                anime_name, slug, mal_id, cover_url, synopsis, total_episodes,
                status_msg, allanime_id=sub_id, audio_type="sub",
            )
            if tok:
                done_sub.append((episode, tok))
                ep_ok = True
            else:
                failed.append(episode)

        # DUB
        if need_dub:
            try:
                await status_msg.edit_text(
                    f"🌐 <b>Batch {i}/{total} — E{episode:02d} [DUB]…</b>\n\n"
                    f"<b>Anime:</b> {anime_name} [{quality}]\n{_status_line()}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            try:
                tok = await _do_one_fetch(
                    bot, anilist_id, episode, quality,
                    anime_name, slug, mal_id, cover_url, synopsis, total_episodes,
                    status_msg, allanime_id=dub_id, audio_type="dub",
                )
                if tok:
                    done_dub.append((episode, tok))
            except Exception as e:
                logger.warning("DUB fetch failed for E%02d — skipping: %s", episode, e)

        if not ep_ok and episode not in failed:
            failed.append(episode)

    lines = [
        f"✅ <b>Batch complete — {anime_name}</b>",
        f"<b>Episodes:</b> E{start_ep:02d}–E{end_ep:02d} | <b>Quality:</b> {quality}",
        _status_line(),
        "",
    ]
    if done_sub:
        lines.append("<b>SUB Player links:</b>")
        for ep_num, tok in done_sub[:6]:
            lines.append(f"• E{ep_num:02d}: <code>{base}/player/{tok}</code>")
        if len(done_sub) > 6:
            lines.append(f"  … and {len(done_sub) - 6} more")
    if done_dub:
        lines.append("\n<b>DUB Player links:</b>")
        for ep_num, tok in done_dub[:6]:
            lines.append(f"• E{ep_num:02d}: <code>{base}/player/{tok}</code>")
        if len(done_dub) > 6:
            lines.append(f"  … and {len(done_dub) - 6} more")
    if not dub_id:
        lines.append("\n<i>ℹ️ DUB not available on AllAnime for this title.</i>")
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
