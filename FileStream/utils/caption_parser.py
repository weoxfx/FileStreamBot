"""
Caption and filename parsers for anime uploads.

AniList ID mode caption:  "12345 | 1 | sub | 720p"
Filename auto mode:       "ReZERO -Starting Life in Another World- - 1 - 360p.mkv"
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

AUDIO_TYPES = {"sub", "dub", "hsub", "multi", "raw"}

# Matches: "Show Name - EpisodeNumber - Quality.ext"
# Show name may contain dashes, so we use non-greedy on the name then require
# a digit-only episode and a quality token before the extension.
_FILENAME_RE = re.compile(
    r"^(.+?)\s+-\s+(\d+)\s+-\s+([\d]+p|4k|2160p)\.\w+$",
    re.IGNORECASE,
)


def parse_caption(caption: str) -> Optional[dict]:
    """
    Parse AniList ID mode caption:
        AniList ID | Episode | sub/dub/hsub | quality
    Returns dict or None.
    """
    if not caption:
        return None
    parts = [p.strip() for p in caption.split("|")]
    if len(parts) < 4:
        logger.warning("Caption needs 4 parts, got %d: %r", len(parts), caption)
        return None
    try:
        anilist_id = int(parts[0])
    except ValueError:
        logger.warning("AniList ID must be an integer, got: %r", parts[0])
        return None
    try:
        episode = int(parts[1])
    except ValueError:
        logger.warning("Episode must be an integer, got: %r", parts[1])
        return None
    audio_type = parts[2].lower()
    quality = parts[3].lower()
    return {
        "anilist_id": anilist_id,
        "episode": episode,
        "audio_type": audio_type,
        "quality": quality,
    }


def parse_filename(filename: str) -> Optional[dict]:
    """
    Parse auto-mode filename:
        Show Name - Episode - Quality.ext
    Example: ReZERO -Starting Life in Another World- - 1 - 360p.mkv
    Returns {anime_name, episode, quality} or None.
    """
    m = _FILENAME_RE.match(filename.strip())
    if not m:
        return None
    return {
        "anime_name": m.group(1).strip(),
        "episode":    int(m.group(2)),
        "quality":    m.group(3).lower(),
    }


def normalize_anime_name(name: str) -> str:
    """Kept for compatibility — use anilist.make_slug for new code."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name.strip("-")
