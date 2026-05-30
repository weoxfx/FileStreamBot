"""
AllAnime API helpers — search and episode list resolution.
Used by the ani-cli handler to reliably resolve show IDs before
handing off to the shell download script.
"""

import logging
import aiohttp
from rapidfuzz import fuzz
from typing import Optional

logger = logging.getLogger(__name__)

_API    = "https://api.allanime.day/api"
_REFR   = "https://youtu-chan.com"
_AGENT  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0"

_SEARCH_GQL = (
    "query($search:SearchInput $limit:Int $page:Int"
    " $translationType:VaildTranslationTypeEnumType"
    " $countryOrigin:VaildCountryOriginEnumType){"
    "shows(search:$search limit:$limit page:$page"
    " translationType:$translationType countryOrigin:$countryOrigin){"
    "edges{_id name availableEpisodes __typename}}}"
)

_EPISODES_GQL = (
    "query($showId:String!){"
    "show(_id:$showId){_id availableEpisodesDetail}}"
)


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Referer": _REFR,
        "User-Agent": _AGENT,
        "Origin": _REFR,
    }


async def search_show(name: str, mode: str = "sub") -> list[dict]:
    """
    Search AllAnime for an anime by name.
    Returns a list of dicts: [{id, name, episode_count}], best match first.
    """
    payload = {
        "variables": {
            "search": {
                "allowAdult": False,
                "allowUnknown": False,
                "query": name,
            },
            "limit": 40,
            "page": 1,
            "translationType": mode,
            "countryOrigin": "ALL",
        },
        "query": _SEARCH_GQL,
    }

    try:
        async with aiohttp.ClientSession(headers=_headers()) as session:
            async with session.post(
                _API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("AllAnime search HTTP %d", resp.status)
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.error("AllAnime search error: %s", e)
        return []

    edges = (
        (data.get("data") or {})
        .get("shows", {})
        .get("edges") or []
    )

    results = []
    for edge in edges:
        show_id = edge.get("_id", "")
        show_name = edge.get("name", "")
        eps = (edge.get("availableEpisodes") or {}).get(mode, 0)
        if show_id and show_name and eps:
            results.append({
                "id":            show_id,
                "name":          show_name,
                "episode_count": eps,
            })

    if not results:
        logger.warning("AllAnime: no results for %r", name)
        return []

    # Sort by fuzzy match against the search name
    query_l = name.lower()
    results.sort(
        key=lambda r: fuzz.token_sort_ratio(query_l, r["name"].lower()),
        reverse=True,
    )

    logger.info(
        "AllAnime search %r → top result: %s (ID: %s, eps: %d)",
        name,
        results[0]["name"],
        results[0]["id"],
        results[0]["episode_count"],
    )
    return results


async def get_episodes_list(show_id: str, mode: str = "sub") -> list[str]:
    """
    Return a sorted list of available episode strings for a show.
    e.g. ["1", "2", "3", ..., "1164"]
    """
    payload = {
        "variables": {"showId": show_id},
        "query": _EPISODES_GQL,
    }

    try:
        async with aiohttp.ClientSession(headers=_headers()) as session:
            async with session.post(
                _API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("AllAnime episodes HTTP %d for %s", resp.status, show_id)
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.error("AllAnime episodes error: %s", e)
        return []

    detail = (
        (data.get("data") or {})
        .get("show", {})
        .get("availableEpisodesDetail") or {}
    )
    ep_list = detail.get(mode) or []

    # Sort numerically
    def _ep_key(ep):
        try:
            return float(ep)
        except (ValueError, TypeError):
            return 0.0

    return sorted(ep_list, key=_ep_key)
