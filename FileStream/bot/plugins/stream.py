# The anime file handler is in anime_handler.py
# This file is kept for non-anime general file uploads (original FileStream behavior).
# It only handles messages from non-authorized senders (so authorized ones go to anime_handler).

from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.utils import bot_db
from pyrogram import filters
from pyrogram.types import Message
from pyrogram.enums.parse_mode import ParseMode


@FileStream.on_message(
    filters.private
    & (filters.video | filters.document),
    group=99,
)
async def fallback_file_handler(bot, message: Message):
    """
    Fallback for authorized users who sent a file without a caption,
    handled by anime_handler already. This catches anyone not in AUTH_USERS.
    """
    user_id = message.from_user.id
    if user_id == Telegram.OWNER_ID or user_id in Telegram.AUTH_USERS:
        return

    await message.reply_text(
        "❌ You are not authorized to upload files to this bot.",
        quote=True
    )
