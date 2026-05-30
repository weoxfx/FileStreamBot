from aiohttp import web
from .api_routes import routes

# 2 GB — allows large file uploads to Telegram via the /upload-file page.
_MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024


def web_server():
    web_app = web.Application(client_max_size=_MAX_UPLOAD_SIZE)
    web_app.add_routes(routes)
    return web_app
