import datetime
from FileStream import __version__
from FileStream.bot import FileStream
from FileStream.config import Telegram, Server
from FileStream.utils.translation import LANG, BUTTON
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums.parse_mode import ParseMode


@FileStream.on_callback_query()
async def cb_data(bot, update: CallbackQuery):
    usr_cmd = update.data.split("_")
    if usr_cmd[0] == "home":
        await update.message.edit_text(
            text=LANG.START_TEXT.format(update.from_user.mention, FileStream.username),
            disable_web_page_preview=True,
            reply_markup=BUTTON.START_BUTTONS
        )
    elif usr_cmd[0] == "help":
        await update.message.edit_text(
            text=LANG.HELP_TEXT.format(Telegram.OWNER_ID),
            disable_web_page_preview=True,
            reply_markup=BUTTON.HELP_BUTTONS
        )
    elif usr_cmd[0] == "about":
        await update.message.edit_text(
            text=LANG.ABOUT_TEXT.format(FileStream.fname, __version__),
            disable_web_page_preview=True,
            reply_markup=BUTTON.ABOUT_BUTTONS
        )
    elif usr_cmd[0] == "N/A":
        await update.answer("N/A", True)
    elif usr_cmd[0] == "close":
        await update.message.delete()
    else:
        await update.answer("Unknown action", True)
