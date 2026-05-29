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
    # Strip HTML from description if any leaked through
    synopsis  = re.sub(r"<[^>]+>", "", media.get("description") or "").strip()
    return {
        "anilist_id":     media["id"],
        "mal_id":         media.get("malId"),
        "name":           title,
        "slug":           make_slug(title),
        "cover_url":      cover_url,
        "banner_url":     media.get("bannerImage") or "",
        "synopsis":       synopsis[:1000],   # cap at 1000 chars
        "total_episodes": media.get("episodes"),
        "score":          media.get("averageScore"),
        "genres":         media.get("genres") or [],
        "status":         media.get("status") or "",
    }


async def _query(payload: dict) -> Optional[dict]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _URL,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                return await r.json()
    except Exception as e:
        logger.warning("AniList request failed: %s", e)
        return None


async def fetch_anime_by_id(anilist_id: int) -> Optional[dict]:
    """Return full metadata dict for a given AniList media ID, or None."""
    data = await _query({"query": _BY_ID, "variables": {"id": anilist_id}})
    if not data:
        return None
    media = data.get("data", {}).get("Media")
    if not media:
        logger.warning("AniList id %s not found", anilist_id)
        return None
    return _parse_media(media)


async def search_anime_by_name(name: str) -> Optional[dict]:
    """Search AniList by title. Returns best match or None."""
    data = await _query({"query": _BY_SEARCH, "variables": {"search": name}})
    if not data:
        return None
    media = data.get("data", {}).get("Media")
    if not media:
        logger.warning("AniList search '%s' returned nothing", name)
        return None
    return _parse_media(media)
