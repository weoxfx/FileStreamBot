"""
AllAnime API helpers — search and episode list resolution.
Used by the ani-cli handler to reliably resolve show IDs before
handing off to the shell download script.
"""

import math
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


async def search_show(
    name: str,
    mode: str = "sub",
    expected_episodes: Optional[int] = None,
) -> list[dict]:
    """
    Search AllAnime for an anime by name.
    Returns a list of dicts: [{id, name, episode_count}], best match first.

    Scoring combines:
    - Exact name match bonus (+100)
    - Fuzzy token-sort ratio (0–100)
    - Episode count bonus:
        * If expected_episodes is known: prefer shows closest to that count (+30 max)
        * If unknown/ongoing: log-scale bonus so long-running series beat short ones (+30 max)

    This ensures that for long-running shows like One Piece (1163 eps), a short
    spin-off with a similar name (12 eps) is always ranked below the main series
    even when AniList doesn't know the total episode count (ongoing shows).
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
        show_id   = edge.get("_id", "")
        show_name = edge.get("name", "")
        eps       = (edge.get("availableEpisodes") or {}).get(mode, 0)
        if show_id and show_name and eps:
            results.append({
                "id":            show_id,
                "name":          show_name,
                "episode_count": eps,
            })

    if not results:
        logger.warning("AllAnime: no results for %r (mode=%s)", name, mode)
        return []

    query_l = name.lower()

    def _score(r: dict) -> float:
        fuzzy = fuzz.token_sort_ratio(query_l, r["name"].lower())
        exact = 100 if r["name"].lower() == query_l else 0

        eps = r["episode_count"]
        if expected_episodes and expected_episodes > 10:
            # Known total: prefer shows close to expected count
            ratio = eps / expected_episodes
            ep_bonus = max(0.0, 30.0 - abs(ratio - 1.0) * 60.0)
        else:
            # Unknown/ongoing: log-scale bonus — 1000 eps ≫ 12 eps
            ep_bonus = min(30.0, math.log10(eps + 1) * 15.0)

        return fuzzy + exact + ep_bonus

    results.sort(key=_score, reverse=True)

    logger.info(
        "AllAnime search %r (mode=%s, expected_eps=%s) → top: %s (ID=%s, eps=%d, score=%.1f)",
        name, mode, expected_episodes,
        results[0]["name"], results[0]["id"], results[0]["episode_count"],
        _score(results[0]),
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

    def _ep_key(ep):
        try:
            return float(ep)
        except (ValueError, TypeError):
            return 0.0

    return sorted(ep_list, key=_ep_key)
