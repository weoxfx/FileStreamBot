import logging
from aiohttp import web
from .api_routes import routes

logger = logging.getLogger(__name__)


async def _restart_bot(app: web.Application):
    from FileStream.bot import FileStream
    from FileStream.bot.clients import initialize_clients

    # If already connected, skip — no need to restart
    if getattr(FileStream, "is_connected", False):
        logger.info("Web startup: bot is already connected as @%s. Skipping restart.",
                    getattr(FileStream, "username", ""))
        return

    try:
        logger.info("Web startup: starting bot client...")
        await FileStream.start()
        bot_info = await FileStream.get_me()
        FileStream.id = bot_info.id
        FileStream.username = bot_info.username
        FileStream.fname = bot_info.first_name
        logger.info("Web startup: bot started as @%s", bot_info.username)
    except Exception as e:
        logger.error("Web startup: failed to start bot client: %s", e)
        return

    try:
        await initialize_clients()
        logger.info("Web startup: multi-clients initialized.")
    except Exception as e:
        logger.error("Web startup: failed to initialize clients: %s", e)


def web_server():
    web_app = web.Application(client_max_size=30000000)
    web_app.add_routes(routes)
    web_app.on_startup.append(_restart_bot)
    return web_app
