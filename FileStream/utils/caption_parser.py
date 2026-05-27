import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

AUDIO_TYPES = {"sub", "dub", "hsub", "multi", "raw"}
QUALITY_MAP = {"360p", "480p", "720p", "1080p", "4k", "2160p"}


def parse_caption(caption: str) -> Optional[dict]:
    """
    Parse anime file caption in the format:
    Anime Name | Season | Episode | sub/dub/hsub | quality
    e.g.: Naruto | 1 | 2 | sub | 720p
    Returns a dict or None if parsing fails.
    """
    if not caption:
        return None

    parts = [p.strip() for p in caption.split("|")]
    if len(parts) < 5:
        logger.warning("Caption does not have 5 parts: %r", caption)
        return None

    anime_name = parts[0].strip()
    if not anime_name:
        return None

    try:
        season = int(parts[1].strip())
    except ValueError:
        logger.warning("Invalid season in caption: %r", parts[1])
        return None

    try:
        episode = int(parts[2].strip())
    except ValueError:
        logger.warning("Invalid episode in caption: %r", parts[2])
        return None

    audio_type = parts[3].strip().lower()
    if audio_type not in AUDIO_TYPES:
        logger.warning("Unknown audio type: %r, accepting anyway", audio_type)

    quality = parts[4].strip().lower()

    return {
        "anime_name": anime_name,
        "season": season,
        "episode": episode,
        "audio_type": audio_type,
        "quality": quality,
    }


def normalize_anime_name(name: str) -> str:
    """Normalize anime name for slug/lookup (lowercase, spaces to hyphens)."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name
