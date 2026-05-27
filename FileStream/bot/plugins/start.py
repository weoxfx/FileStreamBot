import logging
from FileStream import __version__
from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.utils.translation import LANG, BUTTON
from FileStream.utils import bot_db
from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums.parse_mode import ParseMode


@FileStream.on_message(filters.command("start") & filters.private)
async def start(bot: Client, message: Message):
    user_id = message.from_user.id

    if await bot_db.is_banned(user_id):
        await message.reply_text("You are banned from using this bot.", quote=True)
        return

    await bot_db.add_user(user_id)

    if getattr(Telegram, "START_PIC", None):
        await message.reply_photo(
            photo=Telegram.START_PIC,
            caption=LANG.START_TEXT.format(message.from_user.mention, FileStream.username),
            parse_mode=ParseMode.HTML,
            reply_markup=BUTTON.START_BUTTONS
        )
    else:
        await message.reply_text(
            text=LANG.START_TEXT.format(message.from_user.mention, FileStream.username),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=BUTTON.START_BUTTONS
        )


@FileStream.on_message(filters.private & filters.command("about"))
async def about_handler(bot, message):
    await message.reply_text(
        text=LANG.ABOUT_TEXT.format(FileStream.fname, __version__),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=BUTTON.ABOUT_BUTTONS
    )


@FileStream.on_message(filters.command("help") & filters.private)
async def help_handler(bot, message):
    await message.reply_text(
        text=LANG.HELP_TEXT.format(Telegram.OWNER_ID),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=BUTTON.HELP_BUTTONS
    )
