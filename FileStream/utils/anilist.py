"""
AniList GraphQL API helpers.
- fetch_anime_by_id(id)   → {anilist_id, name, slug} or None
- search_anime_by_name(s) → {anilist_id, name, slug} or None
"""
import re
import logging
import aiohttp

from typing import Optional

logger = logging.getLogger(__name__)
_URL = "https://graphql.anilist.co"

_BY_ID = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english }
  }
}
"""

_BY_SEARCH = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english }
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
    """Return {anilist_id, name, slug} for a given AniList media ID, or None."""
    data = await _query({"query": _BY_ID, "variables": {"id": anilist_id}})
    if not data:
        return None
    media = data.get("data", {}).get("Media")
    if not media:
        logger.warning("AniList id %s not found", anilist_id)
        return None
    title = _best_title(media["title"])
    return {"anilist_id": media["id"], "name": title, "slug": make_slug(title)}


async def search_anime_by_name(name: str) -> Optional[dict]:
    """Search AniList by title. Returns best match or None."""
    data = await _query({"query": _BY_SEARCH, "variables": {"search": name}})
    if not data:
        return None
    media = data.get("data", {}).get("Media")
    if not media:
        logger.warning("AniList search '%s' returned nothing", name)
        return None
    title = _best_title(media["title"])
    return {"anilist_id": media["id"], "name": title, "slug": make_slug(title)}
