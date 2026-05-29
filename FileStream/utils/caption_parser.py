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

# Quality tokens we recognise (case-insensitive).
_QUALITY_RE = re.compile(
    r"^(\d{3,4}p|4k|2160p|1080p|720p|480p|360p|240p|bd|web|webrip|hevc|uhd)$",
    re.IGNORECASE,
)

# Video file extensions we accept.
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv"}

# ---------------------------------------------------------------------------
# Caption parser  —  "AniList ID | Episode | sub/dub/hsub | quality"
# ---------------------------------------------------------------------------

def parse_caption(caption: str) -> Optional[dict]:
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

    audio_type = parts[2].lower().strip()
    if audio_type not in AUDIO_TYPES:
        logger.warning(
            "Unknown audio_type %r — expected one of %s. Accepting anyway.",
            audio_type, ", ".join(sorted(AUDIO_TYPES)),
        )

    quality = parts[3].lower().strip()
    if not _QUALITY_RE.match(quality):
        logger.warning("Unrecognised quality token %r — accepting as-is.", quality)

    return {
        "anilist_id": anilist_id,
        "episode":    episode,
        "audio_type": audio_type,
        "quality":    quality,
    }


# ---------------------------------------------------------------------------
# Filename parser  —  "Show Name - Episode - Quality.ext"
# ---------------------------------------------------------------------------

_SEPARATOR = " - "


def parse_filename(filename: str) -> Optional[dict]:
    """
    Parse auto-mode filename:
        Show Name - Episode - Quality.ext

    Handles Telegram double-extensions like 'Show - 1 - 720p.mkv.mp4'
    by stripping all trailing video extensions before parsing.

    Examples that all work:
        ReZERO -Starting Life in Another World- - 1 - 360p.mkv
        ReZERO -Starting Life in Another World- - 1 - 360p.mkv.mp4  ← Telegram mangled
        Attack on Titan - 12 - 1080p.mp4
        Demon Slayer - Kimetsu no Yaiba - 5 - 720p.mkv
        Some Show - 1.5 - BD.mkv
        Some Show - 01 - WEB.mp4
        Some Show - 3 - 4K.mkv
        Some Show - 7 - HEVC.mkv
    """
    filename = filename.strip()

    # Strip ALL trailing extensions (handles .mkv.mp4 double-ext from Telegram)
    base, _ = _strip_video_exts(filename)

    parts = base.split(_SEPARATOR)
    if len(parts) < 3:
        logger.warning(
            "Filename has fewer than 3 ' - ' separated parts: %r", filename
        )
        return None

    # Last part → quality
    quality_raw = parts[-1].strip()
    if not _QUALITY_RE.match(quality_raw):
        logger.warning(
            "Last token %r doesn't look like a quality in filename %r",
            quality_raw, filename,
        )
        return None

    # Second-to-last part → episode number
    episode_raw = parts[-2].strip()
    episode = _parse_episode(episode_raw)
    if episode is None:
        logger.warning(
            "Episode token %r is not a valid number in filename %r",
            episode_raw, filename,
        )
        return None

    # Everything before → anime name
    anime_name = _SEPARATOR.join(parts[:-2]).strip()
    if not anime_name:
        logger.warning("Could not extract anime name from filename %r", filename)
        return None

    return {
        "anime_name": anime_name,
        "episode":    episode,
        "quality":    quality_raw.lower(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_video_exts(filename: str):
    """
    Strip ALL trailing video extensions, including Telegram double-extensions.

    'Show - 1 - 720p.mkv.mp4'  →  ('Show - 1 - 720p', '.mkv')
    'Show - 1 - 720p.mkv'      →  ('Show - 1 - 720p', '.mkv')
    'Show - 1 - 720p'          →  ('Show - 1 - 720p', '')

    Strategy:
      - Always strip the outermost extension on the first pass, even if it's
        not a known video ext (Telegram can append anything like .mp4 on top).
      - Keep peeling as long as extensions are known video formats.
    """
    base = filename
    last_video_ext = ""

    for i in range(6):  # safety cap
        dot_idx = base.rfind(".")
        if dot_idx <= 0:
            break
        ext = base[dot_idx:].lower()
        if ext in _VIDEO_EXTS:
            last_video_ext = ext
            base = base[:dot_idx]
        elif i == 0:
            # First pass only: strip unknown outer ext (e.g. Telegram appended it)
            base = base[:dot_idx]
        else:
            # Inner token is not a video ext — stop peeling
            break

    return base, last_video_ext


def _parse_episode(s: str):
    """
    Parse episode token. Accepts integers and decimals (e.g. "1.5", "01").
    Returns float if decimal, int if whole number, or None on failure.
    """
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def sanitize_search_name(name: str) -> str:
    """
    Clean an anime name for use as an AniList search query.
    AniList rejects queries with certain special characters (returns HTTP 400).
    Removes: parenthetical suffixes, trailing punctuation clusters.
    Keeps:   letters, digits, spaces, single hyphens, colons, apostrophes.
    """
    # Remove anything in parentheses or brackets at the end
    name = re.sub(r"[\(\[].*?[\)\]]", "", name)
    # Remove characters AniList's search chokes on (!, -, dashes at boundaries)
    name = re.sub(r"[^\w\s\-\:\'.]", " ", name)
    # Collapse multiple spaces/hyphens
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"-{2,}", "-", name)
    return name.strip(" -")


def normalize_anime_name(name: str) -> str:
    """Kept for compatibility — use anilist.make_slug for new code."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name.strip("-")
