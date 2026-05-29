"""
ffmpeg watermark pipeline — burns a visible "Tsukuyomi" text watermark
into the top-right corner of every video.

Quality: libx264 CRF 17 (near-lossless). Audio: stream copy.
Falls back to stream-copy (no watermark) if libx264/drawtext unavailable.
"""
import os
import logging
import asyncio
import tempfile
import subprocess
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


def _run_ffmpeg(input_path: str, output_path: str) -> bool:
    """
    Try watermark encode first, then stream-copy fallback.
    Returns True when output_path is ready to upload.
    """
    # ── Step 1: probe file duration so ffmpeg doesn't hang ──────────────────
    # Simple sanity check that the file is readable
    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        logger.error("Input file missing or empty: %s", input_path)
        return False

    # ── Step 2: watermark with drawtext ─────────────────────────────────────
    # Prefer bundled Rajdhani Bold font; fall back to common system fonts
    _font_candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts", "watermark.ttf"),
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    _font_path = next((p for p in _font_candidates if os.path.exists(p)), None)
    _fontfile  = f"fontfile={_font_path}:" if _font_path else ""

    drawtext = (
        "drawtext="
        "text='Tsukuyomi':"
        f"{_fontfile}"
        "fontsize=30:"
        "fontcolor=white@0.40:"
        "shadowcolor=black@0.50:"
        "shadowx=2:"
        "shadowy=2:"
        "x=w-tw-20:"
        "y=18"
    )

    wm_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
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
        r = subprocess.run(
            wm_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=7200,
        )
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Watermark encode succeeded")
            return True
        logger.warning(
            "Watermark encode failed (rc=%d), stderr tail:\n%s",
            r.returncode,
            r.stderr.decode(errors="replace")[-800:],
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg watermark timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found on PATH")
        return False

    # ── Step 3: stream-copy fallback (no watermark, no quality loss) ────────
    logger.info("Falling back to stream-copy remux")
    # Clean up any partial output from failed encode
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
        r2 = subprocess.run(
            cp_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=7200,
        )
        if r2.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info("Stream-copy fallback succeeded (no visible watermark)")
            return True
        logger.error(
            "Stream-copy also failed (rc=%d):\n%s",
            r2.returncode,
            r2.stderr.decode(errors="replace")[-800:],
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg stream-copy timed out")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found")
        return False


async def apply_watermark_and_upload(
    bot_client,
    original_file_id: str,
    original_file_name: str,
    dump_channel_id: int,
    caption: str = "",
    progress_cb=None,
) -> Tuple[Optional[int], Optional[str]]:
    """
    Download → watermark (CRF 17 + drawtext) → faststart → upload.
    Returns (message_id, file_id) or (None, None) on failure.
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
