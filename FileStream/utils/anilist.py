"""
AniList GraphQL API helpers.
Improved search accuracy using multi-result scoring.
"""

import re
import asyncio
import logging
import aiohttp

from typing import Optional
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

_URL = "https://graphql.anilist.co"

_FIELDS = """
    id
    malId
    title {
        romaji
        english
        native
    }
    coverImage {
        extraLarge
        large
    }
    bannerImage
    description(asHtml: false)
    episodes
    averageScore
    genres
    status
    season
    seasonYear
    format
"""

_BY_ID = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
""" + _FIELDS + """
  }
}
"""

# IMPORTANT:
# Use Page.media instead of Media(search:)
# so we can score results ourselves.
_BY_SEARCH = """
query ($search: String) {
  Page(perPage: 15) {
    media(
      search: $search,
      type: ANIME,
      sort: SEARCH_MATCH
    ) {
""" + _FIELDS + """
    }
  }
}
"""


def _best_title(titles: dict) -> str:
    return (
        titles.get("english")
        or titles.get("romaji")
        or titles.get("native")
        or "Unknown"
    ).strip()


def make_slug(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name.strip("-")


def _sanitize_search(name: str) -> str:
    """
    Clean title before AniList search.
    """

    # Remove ONLY trailing bracket groups
    name = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", name)

    # Fix glued dashes
    name = re.sub(r"(?<!\s)-(?=\w)", " ", name)
    name = re.sub(r"(?<=\w)-(?=\s|$)", " ", name)

    # Preserve common anime punctuation
    name = re.sub(r"[^\w\s:\-'/!+.]", " ", name)

    # Collapse spaces
    name = re.sub(r"\s+", " ", name)

    return name.strip()


def _simplify_name(name: str) -> str:
    """
    Simplify subtitle-heavy titles.
    """

    name = re.split(r":\s+", name)[0]
    name = re.split(r"\s+-\s+", name)[0]

    return name.strip()


def _extract_season_info(text: str):
    text = text.lower()

    info = {
        "season_num": None,
        "part_num": None,
        "year": None,
    }

    # 4th Season / Season 4
    patterns = [
        r'(\d+)(st|nd|rd|th)\s+season',
        r'season\s+(\d+)',
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            info["season_num"] = int(m.group(1))
            break

    # Part / Cour
    m = re.search(r'(part|cour)\s*(\d+)', text)
    if m:
        info["part_num"] = int(m.group(2))

    # Year
    m = re.search(r'(19|20)\d{2}', text)
    if m:
        info["year"] = int(m.group(0))

    return info


def _combined_title(media: dict) -> str:
    titles = media.get("title") or {}

    return " ".join(filter(None, [
        titles.get("english"),
        titles.get("romaji"),
        titles.get("native"),
    ])).lower()


def _score_anime_match(query: str, media: dict) -> int:
    """
    Score how well a media entry matches a query.
    Higher = better.
    """

    query_l = query.lower()
    media_title = _combined_title(media)

    # Base fuzzy score
    score = fuzz.token_sort_ratio(query_l, media_title)

    qinfo = _extract_season_info(query_l)
    minfo = _extract_season_info(media_title)

    # Season matching
    if qinfo["season_num"]:
        if qinfo["season_num"] == minfo["season_num"]:
            score += 40
        else:
            score -= 35

    # Part / Cour matching
    if qinfo["part_num"]:
        if qinfo["part_num"] == minfo["part_num"]:
            score += 20
        else:
            score -= 15

    # Year matching
    if qinfo["year"]:
        if qinfo["year"] == media.get("seasonYear"):
            score += 10

    # Important phrases
    important_terms = [
        "final season",
        "second year",
        "first semester",
        "movie",
        "ova",
        "special",
    ]

    for term in important_terms:
        if term in query_l and term in media_title:
            score += 20

    # Penalize movies if query does not mention movie
    if media.get("format") == "MOVIE" and "movie" not in query_l:
        score -= 15

    return score


def _pick_best_match(query: str, results: list[dict]) -> Optional[dict]:
    if not results:
        return None

    scored = []

    for media in results:
        score = _score_anime_match(query, media)

        logger.debug(
            "AniList candidate: %s | score=%s",
            _best_title(media.get("title") or {}),
            score
        )

        scored.append((score, media))

    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best_media = scored[0]

    logger.info(
        "AniList selected: %s (score=%s)",
        _best_title(best_media.get("title") or {}),
        best_score
    )

    return best_media


def _parse_media(media: dict) -> dict:
    title = _best_title(media["title"])

    cover_img = media.get("coverImage") or {}

    cover_url = (
        cover_img.get("extraLarge")
        or cover_img.get("large")
        or ""
    )

    synopsis = re.sub(
        r"<[^>]+>",
        "",
        media.get("description") or ""
    ).strip()

    return {
        "anilist_id": media["id"],
        "mal_id": media.get("malId"),
        "name": title,
        "slug": make_slug(title),
        "cover_url": cover_url,
        "banner_url": media.get("bannerImage") or "",
        "synopsis": synopsis[:1000],
        "total_episodes": media.get("episodes"),
        "score": media.get("averageScore"),
        "genres": media.get("genres") or [],
        "status": media.get("status") or "",
        "season": media.get("season"),
        "season_year": media.get("seasonYear"),
        "format": media.get("format"),
    }


async def _query(payload: dict) -> Optional[dict]:
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
                        "AniList returned HTTP %d for %r",
                        resp.status,
                        payload.get("variables"),
                    )
                    return None

                try:
                    data = await resp.json(content_type=None)
                except Exception as e:
                    logger.warning(
                        "AniList returned invalid JSON: %s",
                        e
                    )
                    return None

                if "errors" in data:
                    logger.warning(
                        "AniList GraphQL errors: %s",
                        data["errors"]
                    )

                return data

    except asyncio.TimeoutError:
        logger.warning("AniList request timed out")
        return None

    except aiohttp.ClientError as e:
        logger.warning("AniList network error: %s", e)
        return None

    except Exception as e:
        logger.warning("AniList unexpected error: %s", e)
        return None


async def fetch_anime_by_id(anilist_id: int) -> Optional[dict]:
    data = await _query({
        "query": _BY_ID,
        "variables": {
            "id": anilist_id
        }
    })

    if not data:
        return None

    media = (data.get("data") or {}).get("Media")

    if not media:
        logger.warning(
            "AniList ID %s not found",
            anilist_id
        )
        return None

    return _parse_media(media)


async def search_anime_by_name(name: str) -> Optional[dict]:
    """
    Multi-stage AniList search with scoring.
    """

    sanitized = _sanitize_search(name)
    simplified = _simplify_name(sanitized)

    candidates = [sanitized]

    if simplified and simplified.lower() != sanitized.lower():
        candidates.append(simplified)

    for attempt, query_name in enumerate(candidates, 1):

        logger.debug(
            "AniList search attempt %d/%d: %r",
            attempt,
            len(candidates),
            query_name,
        )

        data = await _query({
            "query": _BY_SEARCH,
            "variables": {
                "search": query_name
            }
        })

        if not data:
            continue

        media_list = (
            ((data.get("data") or {}).get("Page") or {})
            .get("media")
            or []
        )

        if not media_list:
            logger.warning(
                "AniList search %r returned no media",
                query_name
            )
            continue

        best = _pick_best_match(name, media_list)

        if best:
            return _parse_media(best)

    logger.warning(
        "All AniList search attempts failed for %r",
        name
    )

    return None
