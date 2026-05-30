"""
ffmpeg watermark pipeline — burns a visible "Tsukuyomi" text watermark
into the top-right corner of every video.

Quality: libx264 CRF 23 (fast). Audio: stream copy.
Falls back to stream-copy (no watermark) if libx264/drawtext unavailable.

Public API:
  _run_ffmpeg(input_path, output_path)                  — plain watermark
  run_watermark_with_softsub(input_path, output_path)   — watermark + preserve subtitle tracks
  run_watermark_with_hardsub(input_path, sub_path, output_path) — watermark + burn-in subtitle
"""
import os
import logging
import asyncio
import tempfile
import subprocess
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts", "watermark.ttf"),
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _build_drawtext() -> str:
    """Return the ffmpeg drawtext filter string for the Tsukuyomi watermark."""
    font_path = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
    fontfile  = f"fontfile={font_path}:" if font_path else ""
    return (
        "drawtext="
        "text='Tsukuyomi':"
        f"{fontfile}"
        "fontsize=16:"
        "fontcolor=white@0.35:"
        "shadowcolor=black@0.50:"
        "shadowx=2:"
        "shadowy=2:"
        "x=w-tw-20:"
        "y=18"
    )


def _run_ffmpeg(input_path: str, output_path: str) -> bool:
    """
    Apply Tsukuyomi watermark.
    Try watermark encode first, then stream-copy fallback.
    Returns True when output_path is ready to upload.
    """
    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        logger.error("Input file missing or empty: %s", input_path)
        return False

    drawtext = _build_drawtext()

    wm_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf", drawtext,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]

    logger.info("ffmpeg watermark encode: %s", input_path)
    try:
        r = subprocess.run(wm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Watermark encode succeeded")
            return True
        logger.warning(
            "Watermark encode failed (rc=%d), stderr tail:\n%s",
            r.returncode, r.stderr.decode(errors="replace")[-800:],
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg watermark timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
        return False

    # Stream-copy fallback (no watermark, no quality loss)
    logger.info("Falling back to stream-copy remux")
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass

    cp_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c", "copy",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]
    try:
        r2 = subprocess.run(cp_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if r2.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Stream-copy fallback succeeded")
            return True
        logger.error(
            "Stream-copy also failed (rc=%d):\n%s",
            r2.returncode, r2.stderr.decode(errors="replace")[-800:],
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg stream-copy timed out")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found")
        return False


def run_watermark_with_softsub(input_path: str, output_path: str) -> bool:
    """
    Watermark the video AND preserve any embedded subtitle tracks as mov_text.
    If subtitle conversion fails, falls back to plain watermark (no subs lost — just not muxed).
    Returns True when output_path is ready to upload.
    """
    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        logger.error("Input file missing or empty: %s", input_path)
        return False

    drawtext = _build_drawtext()

    # Try with subtitle track preservation.
    # Explicit -map flags prevent embedded ASS/SSA streams from being pulled
    # into the video filter pipeline (which would cause accidental burn-in).
    cmd_with_subs = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        "-vf", drawtext,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-c:a", "copy",
        "-c:s", "mov_text",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]
    logger.info("ffmpeg watermark+softsub: %s", input_path)
    try:
        r = subprocess.run(cmd_with_subs, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Watermark+softsub encode succeeded")
            return True
        logger.warning(
            "Watermark+softsub failed (rc=%d), falling back to plain watermark:\n%s",
            r.returncode, r.stderr.decode(errors="replace")[-400:],
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg watermark+softsub timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
        return False

    # Clean up any partial output and fall back to plain watermark
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass

    return _run_ffmpeg(input_path, output_path)


def run_watermark_with_hardsub(input_path: str, sub_path: str, output_path: str) -> bool:
    """
    Watermark the video AND burn the given subtitle file into the picture.
    Uses the subtitle + drawtext filters in a single vf chain.
    Returns True on success.
    """
    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        logger.error("Input file missing or empty: %s", input_path)
        return False
    if not os.path.exists(sub_path) or os.path.getsize(sub_path) == 0:
        logger.error("Subtitle file missing or empty: %s", sub_path)
        return False

    drawtext = _build_drawtext()

    _, ext = os.path.splitext(sub_path.lower())
    if ext in (".ass", ".ssa"):
        sub_filter = f"ass={sub_path}"
    else:
        # SRT / VTT — use subtitles filter
        sub_filter = f"subtitles={sub_path}"

    # Put subtitle burn-in BEFORE watermark so the text sits on top
    vf = f"{sub_filter},{drawtext}"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-c:a", "copy",
        "-sn",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]
    logger.info("ffmpeg watermark+hardsub: %s + %s", input_path, sub_path)
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Watermark+hardsub encode succeeded")
            return True
        logger.warning(
            "Watermark+hardsub failed (rc=%d):\n%s",
            r.returncode, r.stderr.decode(errors="replace")[-500:],
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg watermark+hardsub timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
    return False


def run_watermark_with_muxed_sub(input_path: str, sub_path: str, output_path: str) -> bool:
    """
    Watermark the video AND mux an external subtitle file as a soft subtitle track (SUB).
    The output is an MP4 with the Tsukuyomi watermark and the subtitle muxed in as mov_text.
    Falls back to plain watermark if subtitle muxing fails (subtitle still served via API).
    Returns True when output_path is ready to upload.
    """
    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        logger.error("Input file missing or empty: %s", input_path)
        return False
    if not os.path.exists(sub_path) or os.path.getsize(sub_path) == 0:
        logger.error("Subtitle file missing or empty: %s", sub_path)
        return False

    drawtext = _build_drawtext()

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", sub_path,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "1:0",
        "-vf", drawtext,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-c:a", "copy",
        "-c:s", "mov_text",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]
    logger.info("ffmpeg watermark+muxed-sub: %s + %s", input_path, sub_path)
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Watermark+muxed-sub encode succeeded")
            return True
        logger.warning(
            "Watermark+muxed-sub failed (rc=%d), falling back to plain watermark:\n%s",
            r.returncode, r.stderr.decode(errors="replace")[-400:],
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg watermark+muxed-sub timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
        return False

    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass

    return _run_ffmpeg(input_path, output_path)


async def apply_watermark_and_upload(
    bot_client,
    original_file_id: str,
    original_file_name: str,
    dump_channel_id: int,
    caption: str = "",
    progress_cb=None,
) -> Tuple[Optional[int], Optional[str]]:
    """
    Download → watermark (CRF 23 + drawtext) → faststart → upload.
    Returns (message_id, file_id) or (None, None) on failure.
    All processing is done inside a TemporaryDirectory — nothing persists on disk.
    """
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        safe_name = original_file_name.replace("/", "_").replace("\\", "_")
        if not safe_name.lower().endswith(".mp4"):
            safe_name += ".mp4"
        raw_path = os.path.join(tmpdir, "raw_" + safe_name)
        out_path  = os.path.join(tmpdir, "out_" + safe_name)

        logger.info("Downloading: %s", original_file_name)
        try:
            dl_kwargs = {"file_name": raw_path}
            if progress_cb:
                dl_kwargs["progress"] = progress_cb
            dl_path = await bot_client.download_media(original_file_id, **dl_kwargs)
        except Exception as e:
            logger.error("Download failed: %s", e)
            return None, None

        if not dl_path or not os.path.exists(dl_path):
            logger.error("Download returned no file")
            return None, None

        ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, out_path)
        upload_path = out_path if (ok and os.path.exists(out_path)) else dl_path

        logger.info("Uploading to dump channel %s", dump_channel_id)
        try:
            sent = await bot_client.send_video(
                chat_id=dump_channel_id,
                video=upload_path,
                caption=caption,
                supports_streaming=True,
            )
            media = getattr(sent, "video", None) or getattr(sent, "document", None)
            return sent.id, getattr(media, "file_id", None)
        except Exception as e:
            logger.error("Upload failed: %s", e)
            return None, None
