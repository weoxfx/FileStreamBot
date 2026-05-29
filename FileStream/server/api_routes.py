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
  GET /api/anime/{anilist_id}
  GET /api/episodes/{anilist_id}[?episode=N]
  GET /api/qualities/{anilist_id}/{episode}
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


# ── Caches ─────────────────────────────────────────────────────────────────────

# Stream cache: token -> dict with decoded file info, avoids get_messages on every chunk
_stream_cache = {}      # token -> {file_id, mime, size, name, ep}
_STREAM_CACHE_TTL = 3600

# Poster cache: token -> (bytes, timestamp)
_poster_cache = {}      # token -> (bytes, float)
_POSTER_CACHE_TTL = 86400  # 24 hours


def _pick_client():
    index = min(work_loads, key=work_loads.get)
    return index, multi_clients[index]


# ── Public ─────────────────────────────────────────────────────────────────────

@routes.get("/status", allow_head=True)
async def status_handler(_):
    me = FileStream.me
    username = me.username if me else ""
    return web.json_response({
        "server_status": "running",
        "uptime": utils.get_readable_time(time.time() - StartTime),
        "telegram_bot": "@" + username,
        "connected_bots": len(multi_clients),
        "version": __version__,
    })


@routes.get("/player/{token}", allow_head=True)
async def player_handler(request: web.Request):
    """Serve the full-featured Tsukuyomi video player."""
    token = request.match_info["token"]

    ep = await site_db.get_episode_by_token(token)
    if not ep:
        raise web.HTTPNotFound(text="Stream token not found")

    anilist_id = ep.get("anilist_id")

    # Fetch anime metadata for mal_id / cover_url
    anime_meta = None
    if anilist_id:
        anime_meta = await site_db.get_anime_by_anilist_id(anilist_id)

    if anilist_id:
        qualities = await site_db.get_episode_qualities(anilist_id, ep["episode"])
    else:
        qualities = await site_db.get_episode_qualities_by_slug(
            ep["anime_slug"], ep["season"], ep["episode"]
        )

    # Build next-episode token if one exists
    next_token = None
    try:
        if anilist_id:
            all_eps = await site_db.get_episodes_for_anime(anilist_id)
        else:
            all_eps = []
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

    if anilist_id:
        raw_subs = await site_db.get_subtitles_for_episode(anilist_id, ep["episode"])
    else:
        raw_subs = await site_db.get_subtitles_for_episode_by_slug(
            ep["anime_slug"], ep["season"], ep["episode"]
        )
    subtitles = [
        {"id": s["id"], "label": s["label"], "lang": s["lang"], "url": "/subtitle/" + str(s["id"])}
        for s in raw_subs
    ]

    # Pull AniList-sourced fields (prefer DB, fall back to None)
    cover_url  = (anime_meta or {}).get("cover_url") or None
    mal_id_db  = (anime_meta or {}).get("mal_id")
    synopsis   = (anime_meta or {}).get("synopsis") or ""

    # Allow manual ?mal_id= override for legacy links
    mal_id_url = request.rel_url.query.get("mal_id")
    mal_id_val = None
    for candidate in (mal_id_url, mal_id_db):
        try:
            mal_id_val = int(candidate)
            break
        except (TypeError, ValueError):
            pass

    episode_data = {
        "anime_name":     ep["anime_name"],
        "slug":           ep["anime_slug"],
        "anilist_id":     anilist_id,
        "mal_id":         mal_id_val,
        "cover_url":      cover_url,
        "synopsis":       synopsis,
        "season":         ep["season"],
        "episode":        ep["episode"],
        "audio_type":     ep["audio_type"],
        "qualities":      qualities,
        "next_token":     next_token,
        "poster_url":     "/poster/" + token,
        "thumbnails_url": None,
        "subtitles":      subtitles,
    }

    tmpl = _jinja.get_template("player.html")
    html = tmpl.render(
        anime_name   = ep["anime_name"],
        episode      = ep["episode"],
        audio_type   = ep["audio_type"],
        episode_json = json.dumps(episode_data),
    )
    return web.Response(text=html, content_type="text/html")


@routes.get("/poster/{token}", allow_head=True)
async def poster_handler(request: web.Request):
    """
    Serve the video thumbnail/poster from the Telegram dump message.
    Results are cached in memory for 24 hours to avoid repeated Telegram calls.
    """
    token = request.match_info["token"]

    # Serve from cache if available
    cached = _poster_cache.get(token)
    if cached:
        data_bytes, ts = cached
        if time.time() - ts < _POSTER_CACHE_TTL:
            return web.Response(
                body=data_bytes,
                content_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=604800"},
            )
        else:
            del _poster_cache[token]

    try:
        ep = await site_db.get_episode_by_token(token)
        if not ep or not ep.get("dump_msg_id") or not ep.get("dump_channel_id"):
            raise web.HTTPNotFound()

        index, client = _pick_client()

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
        bio = await client.download_media(thumb.file_id, in_memory=True)
        if not bio:
            raise web.HTTPNotFound()

        # BytesIO -> bytes
        if hasattr(bio, "getvalue"):
            data_bytes = bio.getvalue()
        elif hasattr(bio, "read"):
            bio.seek(0)
            data_bytes = bio.read()
        else:
            data_bytes = bytes(bio)

        if not data_bytes:
            raise web.HTTPNotFound()

        # Cache it
        _poster_cache[token] = (data_bytes, time.time())

        return web.Response(
            body=data_bytes,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )
    except web.HTTPException:
        raise
    except Exception as e:
        logger.warning("Poster fetch failed for %s: %s", token, e)
        raise web.HTTPNotFound()


@routes.get("/subtitle/{sub_id}", allow_head=True)
async def subtitle_handler(request: web.Request):
    """
    Stream a subtitle file (VTT/SRT) stored in Telegram by its DB id.
    Cached in memory for 24 hours.
    """
    try:
        sub_id = int(request.match_info["sub_id"])
    except ValueError:
        raise web.HTTPNotFound()

    cache_key = f"sub_{sub_id}"
    cached = _poster_cache.get(cache_key)
    if cached:
        data_bytes, ts = cached
        if time.time() - ts < _POSTER_CACHE_TTL:
            content_type = "text/vtt; charset=utf-8"
            return web.Response(
                body=data_bytes,
                content_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )
        else:
            del _poster_cache[cache_key]

    sub = await site_db.get_subtitle_by_id(sub_id)
    if not sub:
        raise web.HTTPNotFound()

    try:
        _, client = _pick_client()
        bio = await client.download_media(sub["file_id"], in_memory=True)
        if not bio:
            raise web.HTTPNotFound()

        if hasattr(bio, "getvalue"):
            data_bytes = bio.getvalue()
        elif hasattr(bio, "read"):
            bio.seek(0)
            data_bytes = bio.read()
        else:
            data_bytes = bytes(bio)

        if not data_bytes:
            raise web.HTTPNotFound()

        # Convert SRT to VTT on the fly if needed
        text = data_bytes.decode("utf-8", errors="replace")
        if not text.strip().startswith("WEBVTT"):
            text = _srt_to_vtt(text)
            data_bytes = text.encode("utf-8")

        _poster_cache[cache_key] = (data_bytes, time.time())

        return web.Response(
            body=data_bytes,
            content_type="text/vtt; charset=utf-8",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except web.HTTPException:
        raise
    except Exception as e:
        logger.warning("Subtitle fetch failed for id=%s: %s", sub_id, e)
        raise web.HTTPNotFound()


def _srt_to_vtt(srt_text: str) -> str:
    """Convert SRT subtitle format to WebVTT."""
    text = srt_text.replace("\r\n", "\n").replace("\r", "\n")
    # Replace SRT timestamp commas with dots (00:00:00,000 -> 00:00:00.000)
    import re
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text.strip() + "\n"


@routes.get("/api/aniskip", allow_head=True)
async def aniskip_proxy(request: web.Request):
    """
    Proxy to AniSkip API — avoids CORS issues from the browser.
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


async def _get_stream_info(token: str):
    """
    Return cached stream info for a token.
    Fetches from Telegram only on first call; subsequent calls use the cache.
    This prevents O(N) get_messages calls for multi-chunk video streaming.
    """
    now = time.time()
    cached = _stream_cache.get(token)
    if cached and now - cached["ts"] < _STREAM_CACHE_TTL:
        return cached

    ep = await site_db.get_episode_by_token(token)
    if not ep:
        return None

    dump_msg_id     = ep["dump_msg_id"]
    dump_channel_id = ep["dump_channel_id"]
    if not dump_msg_id or not dump_channel_id:
        return None

    index, client = _pick_client()

    msg = await client.get_messages(dump_channel_id, dump_msg_id)
    if not msg:
        return None

    media = getattr(msg, "video", None) or getattr(msg, "document", None)
    if not media:
        return None

    from pyrogram.file_id import FileId
    file_id   = FileId.decode(media.file_id)
    file_size = media.file_size or ep.get("file_size", 0)
    file_name = "{}-s{:02d}e{:02d}-{}-{}.mp4".format(
        ep["anime_slug"], ep["season"], ep["episode"],
        ep["audio_type"], ep["quality"]
    )
    mime = getattr(media, "mime_type", None) or "video/mp4"

    setattr(file_id, "file_size",  file_size)
    setattr(file_id, "mime_type",  mime)
    setattr(file_id, "file_name",  file_name)
    setattr(file_id, "unique_id",  getattr(media, "file_unique_id", ""))

    info = {
        "file_id":   file_id,
        "file_size": file_size,
        "file_name": file_name,
        "mime":      mime,
        "index":     index,
        "ep":        ep,
        "ts":        now,
    }
    _stream_cache[token] = info
    return info


@routes.get("/stream/{token}", allow_head=True)
async def stream_handler(request: web.Request):
    token = request.match_info["token"]

    # Redirect browsers opening the URL directly (not video elements)
    accept = request.headers.get("Accept", "")
    if "text/html" in accept and "video/" not in accept and request.method == "GET":
        raise web.HTTPFound(location=f"/player/{token}")

    try:
        info = await _get_stream_info(token)
        if not info:
            raise web.HTTPNotFound(text="Token not found or no media")

        file_id   = info["file_id"]
        file_size = info["file_size"]
        file_name = info["file_name"]
        mime      = info["mime"]
        index     = info["index"]

        faster_client = multi_clients[index]

        if faster_client not in _class_cache:
            _class_cache[faster_client] = utils.ByteStreamer(faster_client)
        tg_connect = _class_cache[faster_client]

        rng = request.headers.get("Range", "")
        if rng:
            parts = rng.replace("bytes=", "").split("-")
            from_bytes  = int(parts[0])
            until_bytes = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        else:
            from_bytes  = request.http_range.start or 0
            until_bytes = (request.http_range.stop or file_size) - 1

        if until_bytes >= file_size or from_bytes < 0 or until_bytes < from_bytes:
            return web.Response(
                status=416, body="416: Range not satisfiable",
                headers={"Content-Range": "bytes */{}".format(file_size)},
            )

        chunk_size     = 2 * 1024 * 1024
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
    """Same as /stream but forces attachment download."""
    token = request.match_info["token"]
    ep    = await site_db.get_episode_by_token(token)
    if not ep:
        raise web.HTTPNotFound(text="Token not found")
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


@routes.get("/api/anime/{anilist_id}")
@_require_key
async def anime_detail(request: web.Request):
    try:
        anilist_id = int(request.match_info["anilist_id"])
    except ValueError:
        return web.json_response({"error": "anilist_id must be an integer"}, status=400)
    try:
        eps = await site_db.get_episodes_for_anime(anilist_id)
        if not eps:
            return web.json_response({"error": "Not found"}, status=404)
        episode_nums = sorted({ep["episode"] for ep in eps})
        anime = await site_db.get_anime_by_anilist_id(anilist_id)
        return web.json_response({
            "anilist_id": anilist_id,
            "name":       anime["name"] if anime else "",
            "slug":       anime["slug"] if anime else "",
            "episodes":   episode_nums,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/episodes/{anilist_id}")
@_require_key
async def episodes_handler(request: web.Request):
    try:
        anilist_id = int(request.match_info["anilist_id"])
    except ValueError:
        return web.json_response({"error": "anilist_id must be an integer"}, status=400)
    eq = request.rel_url.query.get("episode")
    try:
        if eq is not None:
            episode    = int(eq)
            qualities  = await site_db.get_episode_qualities(anilist_id, episode)
            return web.json_response({
                "anilist_id": anilist_id,
                "episode":    episode,
                "qualities":  qualities,
            })
        eps = await site_db.get_episodes_for_anime(anilist_id)
        return web.json_response({"anilist_id": anilist_id, "episodes": eps})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/qualities/{anilist_id}/{episode}")
@_require_key
async def qualities_handler(request: web.Request):
    try:
        anilist_id = int(request.match_info["anilist_id"])
        episode    = int(request.match_info["episode"])
    except ValueError:
        return web.json_response({"error": "anilist_id/episode must be integers"}, status=400)
    try:
        qs = await site_db.get_episode_qualities(anilist_id, episode)
        if not qs:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response({
            "anilist_id": anilist_id,
            "episode":    episode,
            "qualities":  qs,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
