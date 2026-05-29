from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from FileStream.config import Telegram


class LANG(object):
    START_TEXT = (
        "<b>👋 Hey, {}!</b>\n\n"
        "<b>I'm the Tsukuyomi anime upload bot.</b>\n\n"
        "Use /mode to switch between upload modes:\n\n"
        "<b>AniList ID mode (default):</b>\n"
        "<code>AniList ID | Episode | sub/dub/hsub | quality</code>\n"
        "Example: <code>21355 | 1 | sub | 720p</code>\n\n"
        "<b>Auto Sub / Auto Dub mode:</b>\n"
        "Just send the file — name is parsed automatically:\n"
        "<code>Show Name - Episode - Quality.ext</code>\n\n"
        "<b>@{}</b>"
    )

    HELP_TEXT = (
        "<b>Upload modes — use /mode to switch:</b>\n\n"
        "<b>1. AniList ID mode</b>\n"
        "   Send video with caption:\n"
        "   <code>AniList ID | Episode | sub/dub/hsub | quality</code>\n"
        "   Example: <code>21355 | 1 | sub | 720p</code>\n\n"
        "<b>2. Auto Sub / Auto Dub</b>\n"
        "   Send video with filename:\n"
        "   <code>Show Name - Episode - Quality.ext</code>\n"
        "   Example: <code>ReZERO - 1 - 360p.mkv</code>\n"
        "   Audio type is fixed to sub or dub based on mode.\n\n"
        "<b>Other commands:</b>\n"
        "   /stop — cancel all active uploads\n"
        "   /del &lt;token&gt; — delete an episode\n\n"
        "Contact owner: <a href='tg://user?id={}'>[Owner]</a>"
    )

    ABOUT_TEXT = (
        "<b>⚜ Bot Name: {}</b>\n"
        "<b>✦ Version: {}</b>\n"
        "<b>✦ Watermark: Tsukuyomi</b>\n"
        "<b>✦ DB: SQLite</b>\n"
    )

    BAN_TEXT = (
        "<i>You are banned from using this bot.</i>\n"
        "<b><a href='tg://user?id={}'>Contact Owner</a></b>"
    )


class BUTTON(object):
    START_PIC = getattr(Telegram, "START_PIC", None) or ""
    UPDATES_CHANNEL = getattr(Telegram, "UPDATES_CHANNEL", "Telegram")

    START_BUTTONS = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Help", callback_data="help"),
            InlineKeyboardButton("About", callback_data="about"),
            InlineKeyboardButton("Close", callback_data="close"),
        ],
        [InlineKeyboardButton("📢 Updates Channel", url=f"https://t.me/{UPDATES_CHANNEL}")],
    ])
    HELP_BUTTONS = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Home", callback_data="home"),
            InlineKeyboardButton("About", callback_data="about"),
            InlineKeyboardButton("Close", callback_data="close"),
        ],
        [InlineKeyboardButton("📢 Updates Channel", url=f"https://t.me/{UPDATES_CHANNEL}")],
    ])
    ABOUT_BUTTONS = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Home", callback_data="home"),
            InlineKeyboardButton("Help", callback_data="help"),
            InlineKeyboardButton("Close", callback_data="close"),
        ],
        [InlineKeyboardButton("📢 Updates Channel", url=f"https://t.me/{UPDATES_CHANNEL}")],
    ])
