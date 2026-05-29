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
# Covers: 360p, 480p, 720p, 1080p, 2160p/4K, BD, WEB, WEBRip, HEVC, etc.
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
    """
    Parse AniList ID mode caption:
        AniList ID | Episode | sub/dub/hsub/multi/raw | quality

    - Strips whitespace around each field.
    - Accepts quality strings like 720p, 1080p, 4K, BD, WEB, HEVC …
    - Validates audio_type against AUDIO_TYPES; logs a warning but still
      returns the value so the caller can decide — change `warn_only` to
      False below to make it a hard failure instead.
    - Always returns quality in lowercase.

    Returns dict or None.
    """
    if not caption:
        return None

    parts = [p.strip() for p in caption.split("|")]
    if len(parts) < 4:
        logger.warning("Caption needs 4 parts, got %d: %r", len(parts), caption)
        return None

    # AniList ID
    try:
        anilist_id = int(parts[0])
    except ValueError:
        logger.warning("AniList ID must be an integer, got: %r", parts[0])
        return None

    # Episode
    try:
        episode = int(parts[1])
    except ValueError:
        logger.warning("Episode must be an integer, got: %r", parts[1])
        return None

    # Audio type — normalise to lowercase, warn if unknown
    audio_type = parts[2].lower().strip()
    if audio_type not in AUDIO_TYPES:
        logger.warning(
            "Unknown audio_type %r — expected one of %s. Accepting anyway.",
            audio_type, ", ".join(sorted(AUDIO_TYPES)),
        )

    # Quality — normalise to lowercase, warn if unrecognised pattern
    quality = parts[3].lower().strip()
    if not _QUALITY_RE.match(quality):
        logger.warning(
            "Unrecognised quality token %r — accepting as-is.", quality
        )

    return {
        "anilist_id": anilist_id,
        "episode":    episode,
        "audio_type": audio_type,
        "quality":    quality,
    }


# ---------------------------------------------------------------------------
# Filename parser  —  "Show Name - Episode - Quality.ext"
# ---------------------------------------------------------------------------

# Strategy: split on " - " from the RIGHT so that show names containing
# " - " (e.g. "ReZERO -Starting Life in Another World-") are preserved.
#
# We expect the last two " - "-separated tokens to be:
#   …token[-2] = episode number (digits, optionally with decimal like "1.5")
#   …token[-1] = quality token (then strip extension)
#
# Everything before those two tokens is the anime name.

_SEPARATOR = " - "


def parse_filename(filename: str) -> Optional[dict]:
    """
    Parse auto-mode filename:
        Show Name - Episode - Quality.ext

    Examples that now all work:
        ReZERO -Starting Life in Another World- - 1 - 360p.mkv
        Attack on Titan - 12 - 1080p.mp4
        Demon Slayer - Kimetsu no Yaiba - 5 - 720p.mkv
        Some Show - 1.5 - BD.mkv          ← decimal episodes
        Some Show - 01 - WEB.mp4           ← zero-padded episodes
        Some Show - 3 - 4K.mkv             ← 4K quality
        Some Show - 7 - HEVC.mkv           ← HEVC quality

    Returns {anime_name, episode, quality} or None.
    """
    filename = filename.strip()

    # Strip the file extension (keep it for validation)
    base, ext = _split_ext(filename)
    if ext and ext.lower() not in _VIDEO_EXTS:
        # Not a video extension — still try to parse (Telegram may strip ext)
        logger.warning("Unexpected extension %r in filename %r", ext, filename)

    # Split on " - " and work from the right
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

    # Second-to-last part → episode number (int or decimal like 1.5)
    episode_raw = parts[-2].strip()
    episode = _parse_episode(episode_raw)
    if episode is None:
        logger.warning(
            "Episode token %r is not a valid number in filename %r",
            episode_raw, filename,
        )
        return None

    # Everything before → anime name (re-join with the original separator)
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

def _split_ext(filename: str):
    """Split filename into (base, ext). Returns ('name', '') if no dot."""
    dot_idx = filename.rfind(".")
    if dot_idx == -1 or dot_idx == 0:
        return filename, ""
    return filename[:dot_idx], filename[dot_idx:]


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


def normalize_anime_name(name: str) -> str:
    """Kept for compatibility — use anilist.make_slug for new code."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name.strip("-")
