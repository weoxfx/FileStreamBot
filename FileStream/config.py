from os import environ as env
from dotenv import load_dotenv

load_dotenv()

class Telegram:
    API_ID = int(env.get("API_ID", 0))
    API_HASH = str(env.get("API_HASH", ""))
    BOT_TOKEN = str(env.get("BOT_TOKEN", ""))
    OWNER_ID = int(env.get("OWNER_ID", "0"))
    WORKERS = int(env.get("WORKERS", "6"))
    UPDATES_CHANNEL = str(env.get("UPDATES_CHANNEL", "Telegram"))
    SESSION_NAME = str(env.get("SESSION_NAME", "TsukuyomiBot"))
    FORCE_SUB_ID = env.get("FORCE_SUB_ID", None)
    FORCE_SUB = env.get("FORCE_UPDATES_CHANNEL", False)
    FORCE_SUB = True if str(FORCE_SUB).lower() == "true" else False
    SLEEP_THRESHOLD = int(env.get("SLEEP_THRESHOLD", "60"))
    MULTI_CLIENT = False
    FLOG_CHANNEL = int(env.get("FLOG_CHANNEL", 0))
    ULOG_CHANNEL = int(env.get("ULOG_CHANNEL", 0))
    DUMP_CHANNEL = int(env.get("DUMP_CHANNEL", 0))
    MODE = env.get("MODE", "primary")
    SECONDARY = True if MODE.lower() == "secondary" else False
    AUTH_USERS = list(set(int(x) for x in str(env.get("AUTH_USERS", "")).split() if x))
    DOWNLOAD_LIMIT = int(env.get("DOWNLOAD_LIMIT", "0"))

class Server:
    PORT = int(env.get("PORT", 5000))
    BIND_ADDRESS = str(env.get("BIND_ADDRESS", "0.0.0.0"))
    PING_INTERVAL = int(env.get("PING_INTERVAL", "1200"))
    HAS_SSL = str(env.get("HAS_SSL", "0").lower()) in ("1", "true", "t", "yes", "y")
    NO_PORT = str(env.get("NO_PORT", "0").lower()) in ("1", "true", "t", "yes", "y")
    FQDN = str(env.get("FQDN", BIND_ADDRESS))
    URL = "http{}://{}{}/".format(
        "s" if HAS_SSL else "", FQDN, "" if NO_PORT else ":" + str(PORT)
    )

class Site:
    API_KEY = str(env.get("SITE_API_KEY", "changeme-site-api-key"))
    STREAM_SECRET = str(env.get("STREAM_SECRET", "changeme-stream-secret-32chars!!"))

class BotDB:
    PATH = str(env.get("BOT_DB_PATH", "data/bot.db"))

class SiteDB:
    PATH = str(env.get("SITE_DB_PATH", "data/site.db"))

class Upload:
    PASSWORD = str(env.get("UPLOAD_PASSWORD", "#SuperMoon9559$"))
    SESSION_SECRET = str(env.get("UPLOAD_SESSION_SECRET", Site.STREAM_SECRET))
