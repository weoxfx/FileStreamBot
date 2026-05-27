"""
REST API routes for the website.
All endpoints require the X-API-Key header matching SITE_API_KEY.

GET  /api/anime                    — list all anime
GET  /api/anime/{slug}             — anime detail + season list
GET  /api/episodes/{slug}          — all episodes (optional ?season=N&episode=N)
GET  /api/qualities/{slug}/{s}/{e} — all quality options for one episode
GET  /stream/{token}               — stream the actual video (proxies Telegram)
GET  /status                       — server health (no auth needed)
"""
import time
import math
import logging
import mimetypes
import traceback

from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine

from FileStream.config import Site, Telegram, Server
from FileStream.utils import site_db, bot_db
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream import utils, StartTime, __version__
from FileStream.utils.file_properties import get_file_ids

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _check_api_key(request: web.Request) -> bool:
    key = request.headers.get("X-API-Key", "")
    return key == Site.API_KEY


def _api_key_required(handler):
    async def wrapper(request):
        if not _check_api_key(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)
    return wrapper


@routes.get("/status", allow_head=True)
async def status_handler(_):
    return web.json_response({
        "server_status": "running",
        "uptime": utils.get_readable_time(time.time() - StartTime),
        "telegram_bot": "@" + (FileStream.username or ""),
        "connected_bots": len(multi_clients),
        "version": __version__,
    })


@routes.get("/api/anime")
@_api_key_required
async def list_anime(request: web.Request):
    try:
        anime_list = await site_db.get_anime_list()
        return web.json_response({"anime": anime_list})
    except Exception as e:
        logger.error("list_anime error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/anime/{slug}")
@_api_key_required
async def anime_detail(request: web.Request):
    slug = request.match_info["slug"]
    try:
        episodes = await site_db.get_episodes_for_anime(slug)
        if not episodes:
            return web.json_response({"error": "Not found"}, status=404)

        seasons = {}
        for ep in episodes:
            s = ep["season"]
            if s not in seasons:
                seasons[s] = set()
            seasons[s].add(ep["episode"])

        return web.json_response({
            "slug": slug,
            "seasons": {str(s): sorted(eps) for s, eps in seasons.items()},
        })
    except Exception as e:
        logger.error("anime_detail error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/episodes/{slug}")
@_api_key_required
async def episodes_handler(request: web.Request):
    slug = request.match_info["slug"]
    season_q = request.rel_url.query.get("season")
    episode_q = request.rel_url.query.get("episode")

    try:
        season = int(season_q) if season_q else None
    except ValueError:
        return web.json_response({"error": "season must be an integer"}, status=400)

    try:
        if episode_q and season:
            ep_num = int(episode_q)
            qualities = await site_db.get_episode_qualities(slug, season, ep_num)
            return web.json_response({
                "slug": slug,
                "season": season,
                "episode": ep_num,
                "qualities": qualities,
            })
        else:
            episodes = await site_db.get_episodes_for_anime(slug, season)
            return web.json_response({"slug": slug, "episodes": episodes})
    except Exception as e:
        logger.error("episodes_handler error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/qualities/{slug}/{season}/{episode}")
@_api_key_required
async def qualities_handler(request: web.Request):
    slug = request.match_info["slug"]
    try:
        season = int(request.match_info["season"])
        episode = int(request.match_info["episode"])
    except ValueError:
        return web.json_response({"error": "season and episode must be integers"}, status=400)

    try:
        qualities = await site_db.get_episode_qualities(slug, season, episode)
        if not qualities:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response({
            "slug": slug, "season": season, "episode": episode,
            "qualities": qualities,
        })
    except Exception as e:
        logger.error("qualities_handler error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


class_cache = {}


@routes.get("/stream/{token}", allow_head=True)
@_api_key_required
async def stream_handler(request: web.Request):
    token = request.match_info["token"]
    try:
        ep = await site_db.get_episode_by_token(token)
        if not ep:
            raise web.HTTPNotFound(text="Stream token not found")

        dump_msg_id = ep["dump_msg_id"]
        dump_channel_id = ep["dump_channel_id"]

        if not dump_msg_id or not dump_channel_id:
            raise web.HTTPNotFound(text="Stream source not available")

        index = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]

        if faster_client in class_cache:
            tg_connect = class_cache[faster_client]
        else:
            tg_connect = utils.ByteStreamer(faster_client)
            class_cache[faster_client] = tg_connect

        msg = await faster_client.get_messages(dump_channel_id, dump_msg_id)
        if not msg or not msg.video:
            raise web.HTTPNotFound(text="Dump channel message not found or not a video")

        media = msg.video or msg.document
        file_id_str = media.file_id
        file_size = media.file_size or ep.get("file_size", 0)

        from pyrogram.file_id import FileId
        file_id = FileId.decode(file_id_str)
        setattr(file_id, "file_size", file_size)
        setattr(file_id, "mime_type", getattr(media, "mime_type", "video/mp4"))
        file_name = (
            f"{ep['anime_slug']}-s{ep['season']:02d}e{ep['episode']:02d}"
            f"-{ep['audio_type']}-{ep['quality']}.mp4"
        )
        setattr(file_id, "file_name", file_name)
        setattr(file_id, "unique_id", getattr(media, "file_unique_id", ""))

        range_header = request.headers.get("Range", 0)
        if range_header:
            from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
            from_bytes = int(from_bytes)
            until_bytes = int(until_bytes) if until_bytes else file_size - 1
        else:
            from_bytes = request.http_range.start or 0
            until_bytes = (request.http_range.stop or file_size) - 1

        if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
            return web.Response(
                status=416,
                body="416: Range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        chunk_size = 1024 * 1024
        until_bytes = min(until_bytes, file_size - 1)
        offset = from_bytes - (from_bytes % chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut = until_bytes % chunk_size + 1
        req_length = until_bytes - from_bytes + 1
        part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)

        body = tg_connect.yield_file(
            file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
        )

        mime_type = getattr(media, "mime_type", None) or "video/mp4"

        return web.Response(
            status=206 if range_header else 200,
            body=body,
            headers={
                "Content-Type": mime_type,
                "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
                "Content-Length": str(req_length),
                "Content-Disposition": f'inline; filename="{file_name}"',
                "Accept-Ranges": "bytes",
            },
        )

    except web.HTTPException:
        raise
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        traceback.print_exc()
        logger.critical("stream_handler error: %s", e)
        raise web.HTTPInternalServerError(text=str(e))


@routes.get("/dl/{token}", allow_head=True)
@_api_key_required
async def download_handler(request: web.Request):
    """Same as /stream but forces Content-Disposition: attachment."""
    token = request.match_info["token"]
    request.match_info._route = None
    request = request.clone(rel_url=request.rel_url.with_path(f"/stream/{token}"))
    return await stream_handler(request)
