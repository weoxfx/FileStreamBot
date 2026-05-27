"""
ffmpeg watermark pipeline — burns a visible "Tsukuyomi" text watermark
into the top-right corner of every video.

Quality: libx264 CRF 17 (near-lossless, ~10% larger than CRF 23 default).
Audio:   stream copy (zero audio quality loss).
"""
import os
import logging
import asyncio
import tempfile
import subprocess
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

_WM_TEXT   = "Tsukuyomi"
_WM_COLOR  = "white@0.45"   # semi-transparent white
_WM_SHADOW = "black@0.55"   # shadow for readability on any background


def _build_ffmpeg_cmd(input_path: str, output_path: str) -> list:
    """
    Burn a semi-transparent 'Tsukuyomi' watermark into the top-right corner.
    Uses CRF 17 for near-lossless video quality; audio is stream-copied.

    Requires ffmpeg with libx264 and freetype (drawtext) support.
    Falls back to stream-copy if those are unavailable (watermark skipped).
    """
    # font size = 3% of video height, clamped at reasonable bounds via ffmpeg expr
    fontsize = "max(18\\,min(48\\,trunc(ih*0.033)))"
    pad      = "max(10\\,trunc(ih*0.02))"           # right/top padding

    drawtext = (
        f"drawtext="
        f"text='{_WM_TEXT}':"
        f"fontsize={fontsize}:"
        f"fontcolor={_WM_COLOR}:"
        f"shadowcolor={_WM_SHADOW}:"
        f"shadowx=1:shadowy=1:"
        f"x=w-tw-{pad}:"
        f"y={pad}"
    )

    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", drawtext,
        "-c:v", "libx264",
        "-crf", "17",
        "-preset", "fast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]


def _build_fallback_cmd(input_path: str, output_path: str) -> list:
    """Stream-copy fallback: no watermark, no quality loss."""
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-metadata", "title=Tsukuyomi",
        output_path,
    ]


def _run_ffmpeg(input_path: str, output_path: str) -> bool:
    """
    Run the watermark ffmpeg command.
    If drawtext/libx264 fails (missing filter or codec),
    automatically retries with stream-copy fallback.
    Returns True if a processed file exists at output_path.
    """
    cmd = _build_ffmpeg_cmd(input_path, output_path)
    logger.info("ffmpeg watermark: %s → %s", input_path, output_path)

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=7200,
        )
        if result.returncode == 0 and os.path.exists(output_path):
            logger.info("ffmpeg watermark succeeded")
            return True

        stderr = result.stderr.decode(errors="replace")
        logger.warning("ffmpeg watermark failed (rc=%d), trying fallback.\n%s",
                       result.returncode, stderr[-1500:])

        # Retry with stream-copy (no watermark but no failure)
        fallback = _build_fallback_cmd(input_path, output_path)
        r2 = subprocess.run(
            fallback,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=7200,
        )
        if r2.returncode == 0 and os.path.exists(output_path):
            logger.info("ffmpeg fallback (stream-copy) succeeded")
            return True

        logger.error("ffmpeg fallback also failed:\n%s",
                     r2.stderr.decode(errors="replace")[-1500:])
        return False

    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found — uploading original without watermark")
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
    Download → watermark (CRF 17 + drawtext) → faststart → upload to dump channel.
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
