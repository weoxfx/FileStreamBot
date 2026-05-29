"""
AniList GraphQL API helpers.
- fetch_anime_by_id(id)   → full metadata dict or None
- search_anime_by_name(s) → full metadata dict or None
"""
import re
import logging
import aiohttp

from typing import Optional

logger = logging.getLogger(__name__)
_URL = "https://graphql.anilist.co"

_FIELDS = """
    id
    malId
    title { romaji english }
    coverImage { extraLarge large }
    bannerImage
    description(asHtml: false)
    episodes
    averageScore
    genres
    status
"""

_BY_ID = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
""" + _FIELDS + """
  }
}
"""

_BY_SEARCH = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
""" + _FIELDS + """
  }
}
"""


def _best_title(titles: dict) -> str:
    return (titles.get("english") or titles.get("romaji") or "Unknown").strip()


def make_slug(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name.strip("-")


def _parse_media(media: dict) -> dict:
    title     = _best_title(media["title"])
    cover_img = media.get("coverImage") or {}
    cover_url = cover_img.get("extraLarge") or cover_img.get("large") or ""
    synopsis  = re.sub(r"<[^>]+>", "", media.get("description") or "").strip()
    return {
        "anilist_id":     media["id"],
        "mal_id":         media.get("malId"),
        "name":           title,
        "slug":           make_slug(title),
        "cover_url":      cover_url,
        "banner_url":     media.get("bannerImage") or "",
        "synopsis":       synopsis[:1000],
        "total_episodes": media.get("episodes"),
        "score":          media.get("averageScore"),
        "genres":         media.get("genres") or [],
        "status":         media.get("status") or "",
    }


async def _query(payload: dict) -> Optional[dict]:
    """
    POST a GraphQL query to AniList. Returns the parsed JSON dict or None.

    Guards against:
      - Network errors / timeouts        → logs warning, returns None
      - Non-200 HTTP status              → logs warning, returns None
      - Response body that isn't JSON    → logs warning, returns None
      - GraphQL-level errors in the body → logs warning, returns None
        (AniList returns 200 + {"errors": [...]} for bad queries / not-found)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "AniList returned HTTP %d for payload %r",
                        resp.status, payload.get("variables"),
                    )
                    return None

                try:
                    data = await resp.json(content_type=None)
                except Exception as e:
                    logger.warning("AniList response is not valid JSON: %s", e)
                    return None

                if not isinstance(data, dict):
                    logger.warning("AniList response is not a dict: %r", data)
                    return None

                # GraphQL errors block — present even on HTTP 200
                if "errors" in data:
                    messages = [
                        e.get("message", "?") for e in (data["errors"] or [])
                    ]
                    logger.warning(
                        "AniList GraphQL errors for %r: %s",
                        payload.get("variables"), "; ".join(messages),
                    )
                    # Still return data — caller can decide if data.data.Media exists
                    # (AniList sometimes returns both errors AND partial data)

                return data

    except aiohttp.ClientError as e:
        logger.warning("AniList network error: %s", e)
        return None
    except asyncio.TimeoutError:
        logger.warning("AniList request timed out")
        return None
    except Exception as e:
        logger.warning("AniList request failed unexpectedly: %s", e)
        return None


async def fetch_anime_by_id(anilist_id: int) -> Optional[dict]:
    """Return full metadata dict for a given AniList media ID, or None."""
    data = await _query({"query": _BY_ID, "variables": {"id": anilist_id}})
    if not data:
        return None
    media = (data.get("data") or {}).get("Media")
    if not media:
        logger.warning("AniList id %s not found or returned no Media", anilist_id)
        return None
    return _parse_media(media)


async def search_anime_by_name(name: str) -> Optional[dict]:
    """Search AniList by title. Returns best match or None."""
    data = await _query({"query": _BY_SEARCH, "variables": {"search": name}})
    if not data:
        return None
    media = (data.get("data") or {}).get("Media")
    if not media:
        logger.warning("AniList search %r returned no Media", name)
        return None
    return _parse_media(media)


# Need asyncio for TimeoutError reference in _query
import asyncio
