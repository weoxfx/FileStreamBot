"""
REST API + player routes.

Public (no auth):
  GET /status
  GET /stream/{token}
  GET /dl/{token}
  GET /player/{token}?mal_id=XXX
  GET /api/aniskip?mal_id=X&episode=Y&episode_length=0

API-key protected (X-API-Key header):
  GET /api/anime
  GET /api/anime/{slug}
  GET /api/episodes/{slug}[?season=N[&episode=N]]
  GET /api/qualities/{slug}/{season}/{episode}
"""
import time
import math
import json
import logging
import traceback

import aiohttp
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from jinja2 import Environment, FileSystemLoader, select_autoescape

from FileStream.config import Site, Telegram, Server
from FileStream.utils import site_db, bot_db
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream import utils, StartTime, __version__

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

# Jinja2 env for the player template
_jinja = Environment(
    loader=FileSystemLoader("FileStream/template"),
    autoescape=select_autoescape(["html"]),
)


# ── Auth helper ────────────────────────────────────────────────────────────────

def _check_key(request: web.Request) -> bool:
    return request.headers.get("X-API-Key", "") == Site.API_KEY


def _require_key(fn):
    async def wrapper(request):
        if not _check_key(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await fn(request)
    return wrapper


# ── Public ─────────────────────────────────────────────────────────────────────

@routes.get("/status", allow_head=True)
async def status_handler(_):
    return web.json_response({
        "server_status": "running",
        "uptime": utils.get_readable_time(time.time() - StartTime),
        "telegram_bot": "@" + (FileStream.username or ""),
        "connected_bots": len(multi_clients),
        "version": __version__,
    })


@routes.get("/player/{token}", allow_head=True)
async def player_handler(request: web.Request):
    """Serve the full-featured Tsukuyomi video player."""
    token  = request.match_info["token"]
    mal_id = request.rel_url.query.get("mal_id", "null")

    ep = await site_db.get_episode_by_token(token)
    if not ep:
        raise web.HTTPNotFound(text="Stream token not found")

    qualities = await site_db.get_episode_qualities(
        ep["anime_slug"], ep["season"], ep["episode"]
    )

    # Build next-episode token if one exists
    next_token = None
    try:
        all_eps = await site_db.get_episodes_for_anime(ep["anime_slug"], ep["season"])
        # Find the next episode number (same audio_type preferred)
        next_ep_num = ep["episode"] + 1
        next_candidates = [
            e for e in all_eps
            if e["episode"] == next_ep_num and e["audio_type"] == ep["audio_type"]
        ]
        if not next_candidates:
            next_candidates = [e for e in all_eps if e["episode"] == next_ep_num]
        if next_candidates:
            next_token = next_candidates[0]["stream_token"]
    except Exception:
        pass

    episode_data = {
        "anime_name":  ep["anime_name"],
        "slug":        ep["anime_slug"],
        "season":      ep["season"],
        "episode":     ep["episode"],
        "audio_type":  ep["audio_type"],
        "qualities":   qualities,
        "next_token":  next_token,
        "poster_url":  "/poster/" + token,
    }

    try:
        mal_id_val = int(mal_id)
    except (ValueError, TypeError):
        mal_id_val = "null"

    tmpl = _jinja.get_template("player.html")
    html = tmpl.render(
        anime_name   = ep["anime_name"],
        season       = ep["season"],
        episode      = ep["episode"],
        audio_type   = ep["audio_type"],
        episode_json = json.dumps(episode_data),
        mal_id       = mal_id_val,
        poster_url   = "/poster/" + token,
    )
    return web.Response(text=html, content_type="text/html")


@routes.get("/poster/{token}", allow_head=True)
async def poster_handler(request: web.Request):
    """Serve the video thumbnail/poster image from the Telegram dump message."""
    token = request.match_info["token"]
    try:
        ep = await site_db.get_episode_by_token(token)
        if not ep or not ep.get("dump_msg_id") or not ep.get("dump_channel_id"):
            raise web.HTTPNotFound()

        index = min(work_loads, key=work_loads.get)
        client = multi_clients[index]

        msg = await client.get_messages(ep["dump_channel_id"], ep["dump_msg_id"])
        if not msg:
            raise web.HTTPNotFound()

        media = getattr(msg, "video", None) or getattr(msg, "document", None)
        if not media:
            raise web.HTTPNotFound()

        thumbs = getattr(media, "thumbs", None)
        if not thumbs:
            raise web.HTTPNotFound()

        # Pick the largest thumbnail
        thumb = max(thumbs, key=lambda t: getattr(t, "width", 0) * getattr(t, "height", 0))
        data = await client.download_media(thumb.file_id, in_memory=True)
        if not data:
            raise web.HTTPNotFound()

        return web.Response(
            body=bytes(data),
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )
    except web.HTTPException:
        raise
    except Exception as e:
        logger.warning("Poster fetch failed for %s: %s", token, e)
        raise web.HTTPNotFound()


@routes.get("/api/aniskip", allow_head=True)
async def aniskip_proxy(request: web.Request):
    """
    Proxy to AniSkip API — avoids CORS issues from the browser.
    Query params: mal_id, episode, episode_length (optional, default 0)
    Returns: { results: [{type, startTime, endTime}] }
    """
    mal_id    = request.rel_url.query.get("mal_id", "")
    episode   = request.rel_url.query.get("episode", "1")
    ep_length = request.rel_url.query.get("episode_length", "0")

    if not mal_id:
        return web.json_response({"results": []})

    url = (
        f"https://api.aniskip.com/v2/skip-times/{mal_id}/{episode}"
        f"?types[]=op&types[]=ed&episodeLength={ep_length}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 404:
                    return web.json_response({"results": []})
                data = await r.json()

        # Normalise to [{type, startTime, endTime}]
        results = []
        for item in data.get("results", []):
            skip_type = item.get("skipType") or item.get("type", "")
            interval  = item.get("interval", {})
            results.append({
                "type":      skip_type,
                "startTime": interval.get("startTime", 0),
                "endTime":   interval.get("endTime", 0),
            })

        return web.json_response({"results": results})
    except Exception as e:
        logger.warning("AniSkip fetch failed: %s", e)
        return web.json_response({"results": []})


# ── Stream ─────────────────────────────────────────────────────────────────────

_class_cache = {}


@routes.get("/stream/{token}", allow_head=True)
async def stream_handler(request: web.Request):
    token = request.match_info["token"]

    # Redirect browsers to the player page instead of raw bytes
    accept = request.headers.get("Accept", "")
    if "text/html" in accept and request.method == "GET":
        raise web.HTTPFound(location=f"/player/{token}")

    try:
        ep = await site_db.get_episode_by_token(token)
        if not ep:
            raise web.HTTPNotFound(text="Token not found")

        dump_msg_id    = ep["dump_msg_id"]
        dump_channel_id = ep["dump_channel_id"]
        if not dump_msg_id or not dump_channel_id:
            raise web.HTTPNotFound(text="Stream source not set")

        index         = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]

        if faster_client not in _class_cache:
            _class_cache[faster_client] = utils.ByteStreamer(faster_client)
        tg_connect = _class_cache[faster_client]

        msg = await faster_client.get_messages(dump_channel_id, dump_msg_id)
        if not msg:
            raise web.HTTPNotFound(text="Dump message not found")

        media = getattr(msg, "video", None) or getattr(msg, "document", None)
        if not media:
            raise web.HTTPNotFound(text="No media in dump message")

        from pyrogram.file_id import FileId
        file_id = FileId.decode(media.file_id)
        file_size = media.file_size or ep.get("file_size", 0)
        file_name = "{}-s{:02d}e{:02d}-{}-{}.mp4".format(
            ep["anime_slug"], ep["season"], ep["episode"],
            ep["audio_type"], ep["quality"]
        )
        setattr(file_id, "file_size",  file_size)
        setattr(file_id, "mime_type",  getattr(media, "mime_type", "video/mp4"))
        setattr(file_id, "file_name",  file_name)
        setattr(file_id, "unique_id",  getattr(media, "file_unique_id", ""))

        rng = request.headers.get("Range", "")
        if rng:
            parts = rng.replace("bytes=", "").split("-")
            from_bytes  = int(parts[0])
            until_bytes = int(parts[1]) if parts[1] else file_size - 1
        else:
            from_bytes  = request.http_range.start or 0
            until_bytes = (request.http_range.stop or file_size) - 1

        if until_bytes > file_size or from_bytes < 0 or until_bytes < from_bytes:
            return web.Response(
                status=416, body="416: Range not satisfiable",
                headers={"Content-Range": "bytes */{}".format(file_size)},
            )

        chunk_size     = 1024 * 1024
        until_bytes    = min(until_bytes, file_size - 1)
        offset         = from_bytes - (from_bytes % chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut  = until_bytes % chunk_size + 1
        req_length     = until_bytes - from_bytes + 1
        part_count     = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)

        body = tg_connect.yield_file(
            file_id, index, offset,
            first_part_cut, last_part_cut, part_count, chunk_size
        )
        mime = getattr(media, "mime_type", None) or "video/mp4"

        return web.Response(
            status=206 if rng else 200,
            body=body,
            headers={
                "Content-Type":        mime,
                "Content-Range":       "bytes {}-{}/{}".format(from_bytes, until_bytes, file_size),
                "Content-Length":      str(req_length),
                "Content-Disposition": 'inline; filename="{}"'.format(file_name),
                "Accept-Ranges":       "bytes",
            },
        )

    except web.HTTPException:
        raise
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        traceback.print_exc()
        raise web.HTTPInternalServerError(text=str(e))


@routes.get("/dl/{token}", allow_head=True)
async def download_handler(request: web.Request):
    """Same as /stream but attachment disposition."""
    token = request.match_info["token"]
    ep    = await site_db.get_episode_by_token(token)
    if not ep:
        raise web.HTTPNotFound(text="Token not found")
    # Re-use stream_handler with the same token but different path
    new_req = request.clone(
        rel_url=request.rel_url.with_path("/stream/" + token)
    )
    resp = await stream_handler(new_req)
    if hasattr(resp, "headers"):
        file_name = "{}-s{:02d}e{:02d}-{}-{}.mp4".format(
            ep["anime_slug"], ep["season"], ep["episode"],
            ep["audio_type"], ep["quality"]
        )
        resp.headers["Content-Disposition"] = 'attachment; filename="{}"'.format(file_name)
    return resp


# ── Site API (key-protected) ────────────────────────────────────────────────────

@routes.get("/api/anime")
@_require_key
async def list_anime(request: web.Request):
    try:
        return web.json_response({"anime": await site_db.get_anime_list()})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/anime/{slug}")
@_require_key
async def anime_detail(request: web.Request):
    slug = request.match_info["slug"]
    try:
        eps = await site_db.get_episodes_for_anime(slug)
        if not eps:
            return web.json_response({"error": "Not found"}, status=404)
        seasons = {}
        for ep in eps:
            s = ep["season"]
            seasons.setdefault(s, set()).add(ep["episode"])
        return web.json_response({
            "slug": slug,
            "seasons": {str(s): sorted(v) for s, v in seasons.items()},
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/episodes/{slug}")
@_require_key
async def episodes_handler(request: web.Request):
    slug = request.match_info["slug"]
    sq   = request.rel_url.query.get("season")
    eq   = request.rel_url.query.get("episode")
    try:
        season = int(sq) if sq else None
    except ValueError:
        return web.json_response({"error": "season must be integer"}, status=400)
    try:
        if eq and season is not None:
            qualities = await site_db.get_episode_qualities(slug, season, int(eq))
            return web.json_response({
                "slug": slug, "season": season, "episode": int(eq),
                "qualities": qualities,
            })
        eps = await site_db.get_episodes_for_anime(slug, season)
        return web.json_response({"slug": slug, "episodes": eps})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/qualities/{slug}/{season}/{episode}")
@_require_key
async def qualities_handler(request: web.Request):
    slug = request.match_info["slug"]
    try:
        season  = int(request.match_info["season"])
        episode = int(request.match_info["episode"])
    except ValueError:
        return web.json_response({"error": "season/episode must be integers"}, status=400)
    try:
        qs = await site_db.get_episode_qualities(slug, season, episode)
        if not qs:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response({
            "slug": slug, "season": season, "episode": episode, "qualities": qs,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
