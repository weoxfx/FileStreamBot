from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from FileStream.config import Telegram


class LANG(object):
    START_TEXT = (
        "<b>👋 Hey, {}!</b>\n\n"
        "<b>I'm the Tsukuyomi anime upload bot.</b>\n"
        "Send me a video file with a caption in this format:\n\n"
        "<code>Anime Name | Season | Episode | sub/dub/hsub | quality</code>\n\n"
        "Example:\n"
        "<code>Naruto | 1 | 2 | sub | 720p</code>\n\n"
        "<b>@{}</b>"
    )

    HELP_TEXT = (
        "<b>How to upload an anime episode:</b>\n\n"
        "1. Send a video file with this caption:\n"
        "   <code>Anime Name | Season | Episode | sub/dub/hsub | quality</code>\n\n"
        "2. The bot will:\n"
        "   • Apply the Tsukuyomi watermark\n"
        "   • Upload to the dump channel\n"
        "   • Give you a stream token for the website\n\n"
        "<b>Audio types:</b> sub, dub, hsub, multi, raw\n"
        "<b>Quality:</b> 360p, 480p, 720p, 1080p\n\n"
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
