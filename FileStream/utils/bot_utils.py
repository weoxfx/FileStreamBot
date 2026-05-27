# Legacy bot_utils — kept as a stub so old imports don't break.
# New logic is in anime_handler.py, bot_db.py, and site_db.py.

from FileStream.utils import bot_db
from FileStream.config import Telegram


async def is_user_banned(message) -> bool:
    return await bot_db.is_banned(message.from_user.id)


async def is_user_authorized(message) -> bool:
    user_id = message.from_user.id
    if user_id == Telegram.OWNER_ID:
        return True
    if Telegram.AUTH_USERS and user_id not in Telegram.AUTH_USERS:
        return False
    return True


async def is_user_exist(bot, message):
    await bot_db.add_user(message.from_user.id)


async def verify_user(bot, message) -> bool:
    if not await is_user_authorized(message):
        return False
    if await is_user_banned(message):
        return False
    await is_user_exist(bot, message)
    return True
