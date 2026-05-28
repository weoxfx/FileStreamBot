import logging
from aiohttp import web
from .api_routes import routes

logger = logging.getLogger(__name__)


async def _restart_bot(app: web.Application):
    from FileStream.bot import FileStream
    from FileStream.bot.clients import initialize_clients

    try:
        if FileStream.is_connected:
            logger.info("Web startup: stopping bot client for restart...")
            await FileStream.stop()
    except Exception as e:
        logger.warning("Web startup: error stopping bot client: %s", e)

    try:
        logger.info("Web startup: starting bot client...")
        await FileStream.start()
        bot_info = await FileStream.get_me()
        FileStream.id = bot_info.id
        FileStream.username = bot_info.username
        FileStream.fname = bot_info.first_name
        logger.info("Web startup: bot restarted as @%s", bot_info.username)
    except Exception as e:
        logger.error("Web startup: failed to restart bot client: %s", e)
        return

    try:
        await initialize_clients()
        logger.info("Web startup: multi-clients re-initialized.")
    except Exception as e:
        logger.error("Web startup: failed to re-initialize clients: %s", e)


def web_server():
    web_app = web.Application(client_max_size=30000000)
    web_app.add_routes(routes)
    web_app.on_startup.append(_restart_bot)
    return web_app
