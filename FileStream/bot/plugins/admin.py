import os
import time
import string
import random
import asyncio
import aiofiles
import datetime

from FileStream.utils.broadcast_helper import send_msg
from FileStream.utils import bot_db
from FileStream.utils.site_db import get_episode_by_token, delete_episode_by_token
from FileStream.bot import FileStream
from FileStream.config import Telegram, Server, Site
from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums.parse_mode import ParseMode

broadcast_ids = {}


@FileStream.on_message(filters.command("status") & filters.private & filters.user(Telegram.OWNER_ID))
async def admin_status(c: Client, m: Message):
    total = await bot_db.get_total_users()
    banned = await bot_db.get_banned_count()
    await m.reply_text(
        f"**Total Users:** `{total}`\n"
        f"**Banned Users:** `{banned}`",
        parse_mode=ParseMode.MARKDOWN,
        quote=True
    )


@FileStream.on_message(filters.command("ban") & filters.private & filters.user(Telegram.OWNER_ID))
async def admin_ban(b: Client, m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.reply_text("Usage: /ban <user_id>", quote=True)
        return
    uid = int(parts[1])
    if await bot_db.is_banned(uid):
        await m.reply_text(f"`{uid}` is already banned.", parse_mode=ParseMode.MARKDOWN, quote=True)
        return
    await bot_db.ban_user(uid)
    await m.reply_text(f"`{uid}` has been **banned**.", parse_mode=ParseMode.MARKDOWN, quote=True)
    try:
        await b.send_message(uid, "You have been banned from using this bot.")
    except Exception:
        pass


@FileStream.on_message(filters.command("unban") & filters.private & filters.user(Telegram.OWNER_ID))
async def admin_unban(b: Client, m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.reply_text("Usage: /unban <user_id>", quote=True)
        return
    uid = int(parts[1])
    if not await bot_db.is_banned(uid):
        await m.reply_text(f"`{uid}` is not banned.", parse_mode=ParseMode.MARKDOWN, quote=True)
        return
    await bot_db.unban_user(uid)
    await m.reply_text(f"`{uid}` has been **unbanned**.", parse_mode=ParseMode.MARKDOWN, quote=True)
    try:
        await b.send_message(uid, "You have been unbanned. You can use the bot again.")
    except Exception:
        pass


@FileStream.on_message(filters.command("logs") & filters.private & filters.user(Telegram.OWNER_ID))
async def admin_logs(c: Client, m: Message):
    logs = await bot_db.get_recent_logs(50)
    if not logs:
        await m.reply_text("No logs yet.", quote=True)
        return
    lines = []
    for log in logs[:20]:
        ts = datetime.datetime.fromtimestamp(log["created_at"]).strftime("%d/%m %H:%M")
        lines.append(f"[{ts}] {log['level']}: {log['message'][:120]}")
    text = "\n".join(lines)
    await m.reply_text(f"```\n{text}\n```", parse_mode=ParseMode.MARKDOWN, quote=True)


@FileStream.on_message(filters.command("apikey") & filters.private & filters.user(Telegram.OWNER_ID))
async def admin_apikey(c: Client, m: Message):
    await m.reply_text(
        f"**Site API Key:**\n`{Site.API_KEY}`\n\n"
        f"Set `X-API-Key: {Site.API_KEY}` in your website requests.",
        parse_mode=ParseMode.MARKDOWN,
        quote=True
    )


@FileStream.on_message(filters.command("del") & filters.private & filters.user(Telegram.OWNER_ID))
async def admin_del(c: Client, m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.reply_text(
            "**Usage:** `/del <stream_token>`\n\n"
            "Looks up the episode by token, removes it from the database, "
            "and deletes the file from the dump channel.",
            parse_mode=ParseMode.MARKDOWN,
            quote=True
        )
        return

    token = parts[1].strip()
    ep = await get_episode_by_token(token)
    if not ep:
        await m.reply_text(
            f"No episode found with token `{token}`.",
            parse_mode=ParseMode.MARKDOWN,
            quote=True
        )
        return

    info = (
        f"**Found episode — deleting…**\n"
        f"Anime: `{ep['anime_name']}`\n"
        f"Season `{ep['season']}` · Episode `{ep['episode']}`\n"
        f"Quality: `{ep['quality']}` · Audio: `{ep['audio_type']}`\n"
        f"File size: `{ep.get('file_size', 0) // 1_000_000} MB`"
    )
    msg = await m.reply_text(info, parse_mode=ParseMode.MARKDOWN, quote=True)

    deleted = await delete_episode_by_token(token)
    if not deleted:
        await msg.edit_text("❌ Delete failed — episode may have already been removed.")
        return

    dump_note = "n/a"
    if ep.get("dump_msg_id") and ep.get("dump_channel_id"):
        try:
            await c.delete_messages(int(ep["dump_channel_id"]), int(ep["dump_msg_id"]))
            dump_note = "deleted from channel"
        except Exception:
            dump_note = "could not delete from channel (no permission or already gone)"

    await msg.edit_text(
        f"✅ **Deleted:** `{ep['anime_name']}` "
        f"S{str(ep['season']).zfill(2)}E{str(ep['episode']).zfill(2)} "
        f"({ep['quality']} / {ep['audio_type']})\n"
        f"Dump channel: {dump_note}",
        parse_mode=ParseMode.MARKDOWN
    )


@FileStream.on_message(filters.command("broadcast") & filters.private & filters.user(Telegram.OWNER_ID) & filters.reply)
async def broadcast_(c: Client, m: Message):
    all_users = await bot_db.get_all_users()
    broadcast_msg = m.reply_to_message
    while True:
        broadcast_id = "".join([random.choice(string.ascii_letters) for _ in range(3)])
        if not broadcast_ids.get(broadcast_id):
            break
    out = await m.reply_text("Broadcast initiated! You will be notified when done.")
    start_time = time.time()
    total_users = len(all_users)
    done = 0
    failed = 0
    success = 0
    broadcast_ids[broadcast_id] = dict(total=total_users, current=done, failed=failed, success=success)

    async with aiofiles.open("broadcast.txt", "w") as bf:
        for user in all_users:
            sts, msg = await send_msg(user_id=int(user["id"]), message=broadcast_msg)
            if msg is not None:
                await bf.write(msg)
            if sts == 200:
                success += 1
            else:
                failed += 1
            done += 1
            if broadcast_ids.get(broadcast_id) is None:
                break
            broadcast_ids[broadcast_id].update(dict(current=done, failed=failed, success=success))
            try:
                await out.edit_text(f"Broadcast: {done}/{total_users} | ✅ {success} | ❌ {failed}")
            except Exception:
                pass

    if broadcast_ids.get(broadcast_id):
        broadcast_ids.pop(broadcast_id)
    completed_in = datetime.timedelta(seconds=int(time.time() - start_time))
    await asyncio.sleep(3)
    await out.delete()
    if failed == 0:
        await m.reply_text(
            f"Broadcast done in `{completed_in}`\n{total_users} users | {success} success | {failed} failed.",
            quote=True
        )
    else:
        await m.reply_document(
            document="broadcast.txt",
            caption=f"Broadcast done in `{completed_in}`\n{total_users} users | {success} success | {failed} failed.",
            quote=True
        )
    if os.path.exists("broadcast.txt"):
        os.remove("broadcast.txt")
