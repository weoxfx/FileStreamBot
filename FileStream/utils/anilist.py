"""
AniList GraphQL API helpers.
- fetch_anime_by_id(id)   → full metadata dict or None
- search_anime_by_name(s) → full metadata dict or None
"""
import re
import asyncio
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

def _sanitize_search(name: str) -> str:
    """
    Clean a name before sending to AniList search.
    AniList returns HTTP 400 for queries with dashes directly touching words
    e.g. '-Starting' or 'World-', special chars, or unmatched brackets.
    """
    # Drop anything in brackets/parens
    name = re.sub(r"[\(\[].*?[\)\]]", "", name)
    # Dashes touching a word on either side → space
    # e.g. '-Starting' → ' Starting',  'World-' → 'World '
    name = re.sub(r"(?<!\s)-(?=\w)", " ", name)
    name = re.sub(r"(?<=\w)-(?=\s|$)", " ", name)
    # Drop remaining special chars except colon and apostrophe
    name = re.sub(r"[^\w\s\:\']", " ", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _simplify_name(name: str) -> str:
    """
    Drop subtitle portions for a shorter fallback search.
    'Made in Abyss: The Golden City' → 'Made in Abyss'
    'Sword Art Online: Alicization'  → 'Sword Art Online'
    """
    # Drop after ': '
    name = re.split(r":\s+", name)[0]
    # Drop after ' - '
    name = re.split(r"\s+-\s+", name)[0]
    return name.strip()

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
    POST a GraphQL query to AniList. Returns parsed JSON dict or None.
    None means a hard failure (network, timeout, bad status, unparseable body).
    Callers must handle GraphQL-level not-found via data.get("data","").get("Media").
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

                if "errors" in data:
                    messages = [e.get("message", "?") for e in (data["errors"] or [])]
                    logger.warning(
                        "AniList GraphQL errors for %r: %s",
                        payload.get("variables"), "; ".join(messages),
                    )

                return data

    except (aiohttp.ClientError, aiohttp.ServerDisconnectedError) as e:
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
    """
    Search AniList by title. Returns best match or None.

    Attempt order:
      1. Sanitized full name       e.g. "ReZERO Starting Life in Another World"
      2. Simplified name (no sub)  e.g. "ReZERO"
    This handles AniList HTTP 400 errors caused by special characters or
    overly long/complex search strings.
    """
    sanitized = _sanitize_search(name)
    simplified = _simplify_name(sanitized)

    candidates = [sanitized]
    if simplified and simplified.lower() != sanitized.lower():
        candidates.append(simplified)

    for attempt, query_name in enumerate(candidates, 1):
        logger.debug(
            "AniList search attempt %d/%d: %r", attempt, len(candidates), query_name
        )
        data = await _query({"query": _BY_SEARCH, "variables": {"search": query_name}})
        if not data:
            # Hard failure (network/400/timeout) — try next candidate
            continue
        media = (data.get("data") or {}).get("Media")
        if media:
            return _parse_media(media)
        logger.warning("AniList search %r returned no Media", query_name)

    logger.warning("All AniList search attempts failed for original name %r", name)
    return None
