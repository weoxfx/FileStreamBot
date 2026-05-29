import sys
import asyncio
import logging
import traceback
import logging.handlers as handlers
import os

from FileStream.config import Telegram, Server
from aiohttp import web
from pyrogram import idle

from FileStream.bot import FileStream
from FileStream.server import web_server
from FileStream.bot.clients import initialize_clients
from FileStream.utils import bot_db, site_db

os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    datefmt="%d/%m/%Y %H:%M:%S",
    format="[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout),
        handlers.RotatingFileHandler(
            "logs/streambot.log", mode="a", maxBytes=104857600, backupCount=2, encoding="utf-8"
        ),
    ],
)

logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

server = web.AppRunner(web_server())

loop = asyncio.get_event_loop()

bot_connected = False


async def start_services():
    global bot_connected

    print()
    print("-------------- Tsukuyomi Bot Starting --------------")
    print()

    print("Initializing databases...")
    await bot_db.init_bot_db()
    await site_db.init_site_db()
    print("Databases ready.")
    print()

    print(f"Starting web server on {Server.BIND_ADDRESS}:{Server.PORT}...")
    await server.setup()
    await web.TCPSite(server, Server.BIND_ADDRESS, Server.PORT).start()
    print(f"Server URL: {Server.URL}")
    print("Web server is running.")
    print()

    if not Telegram.API_ID or not Telegram.API_HASH or not Telegram.BOT_TOKEN:
        print("WARNING: Telegram credentials not set — bot disabled, player/web UI still available.")
        print("---------------- Web Server Running (Bot Offline) ---------------")
        while True:
            await asyncio.sleep(3600)
        return

    print("Connecting Telegram bot...")
    try:
        await FileStream.start()
        bot_info = await FileStream.get_me()
        FileStream.id = bot_info.id
        FileStream.username = bot_info.username
        FileStream.fname = bot_info.first_name
        print(f"Bot: @{bot_info.username}")
        bot_connected = True
    except Exception as e:
        print(f"WARNING: Telegram bot failed to connect ({e})")
        print("Player and web UI are still available.")
        print("---------------- Web Server Running (Bot Offline) ---------------")
        while True:
            await asyncio.sleep(3600)
        return

    print()
    print("Initializing multi-clients...")
    await initialize_clients()
    print()

    print("---------------- All Services Running ---------------")
    await idle()


async def cleanup():
    await server.cleanup()
    if bot_connected:
        try:
            await FileStream.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        pass
    except Exception as err:
        logging.error(traceback.format_exc())
    finally:
        loop.run_until_complete(cleanup())
        loop.stop()
        print("----------------- Services Stopped -----------------")
