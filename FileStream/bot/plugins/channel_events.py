"""
Handles events in the dump channel.

When a message is deleted from the dump channel, the corresponding
episode is automatically removed from the database.
"""
import logging

from pyrogram import raw
from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.utils import site_db
from FileStream.server.api_routes import _poster_cache, _stream_cache

logger = logging.getLogger(__name__)


@FileStream.on_raw_update()
async def raw_update_handler(client, update, users, chats):
    """
    Listen for deleted channel messages.
    Pyrogram fires UpdateDeleteChannelMessages when messages are deleted in a channel.
    """
    if not isinstance(update, raw.types.UpdateDeleteChannelMessages):
        return

    # The channel_id in the raw update is without the -100 prefix
    channel_id = int(f"-100{update.channel_id}")

    # Only care about our dump channel
    dump_channel = Telegram.DUMP_CHANNEL
    if not dump_channel or channel_id != dump_channel:
        return

    deleted_ids = update.messages
    if not deleted_ids:
        return

    logger.info(
        "Dump channel: %d message(s) deleted — cleaning up DB",
        len(deleted_ids)
    )

    for msg_id in deleted_ids:
        try:
            deleted = await site_db.delete_episode_by_dump_msg(msg_id, channel_id)
            if deleted:
                logger.info("Removed episode for dump_msg_id=%s", msg_id)
                # Also evict caches so stale data isn't served
                _poster_cache.pop(str(msg_id), None)
                # Stream cache is keyed by stream_token, not msg_id — evict by scan
                to_evict = [
                    tok for tok, info in list(_stream_cache.items())
                    if info.get("ep", {}).get("dump_msg_id") == msg_id
                ]
                for tok in to_evict:
                    _stream_cache.pop(tok, None)
        except Exception as e:
            logger.warning("Error removing episode for msg_id=%s: %s", msg_id, e)
