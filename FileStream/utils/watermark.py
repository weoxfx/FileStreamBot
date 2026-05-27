"""
ffmpeg watermark pipeline — Tsukuyomi watermark, top-right, small, semi-transparent.
Downloads from Telegram, burns watermark, re-uploads to dump channel.
"""
import os
import asyncio
import logging
import tempfile
import subprocess
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


def _build_ffmpeg_cmd(input_path: str, output_path: str):
    # Small, clean watermark — fontsize 20, opacity 0.38, top-right corner
    drawtext = (
        "drawtext=text='Tsukuyomi'"
        ":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        ":fontsize=20"
        ":fontcolor=white@0.38"
        ":x=w-tw-14"
        ":y=12"
        ":shadowcolor=black@0.4"
        ":shadowx=1:shadowy=1"
    )
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", drawtext,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",          # higher quality (was 22)
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path
    ]


def _run_ffmpeg(input_path: str, output_path: str) -> bool:
    cmd = _build_ffmpeg_cmd(input_path, output_path)
    logger.info("ffmpeg watermark: %s → %s", input_path, output_path)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=7200
        )
        if result.returncode != 0:
            logger.error("ffmpeg failed:\n%s", result.stderr.decode(errors="replace")[-2000:])
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out")
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
    Download → watermark → upload to dump channel.
    progress_cb(current, total) called during download if provided.
    Returns (message_id, file_id) or (None, None) on failure.
    """
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        safe_name = original_file_name.replace("/", "_").replace("\\", "_")
        raw_path = os.path.join(tmpdir, "raw_" + safe_name)
        wm_path  = os.path.join(tmpdir, "wm_"  + safe_name)

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

        ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
        upload_path = wm_path if (ok and os.path.exists(wm_path)) else dl_path

        logger.info("Uploading to dump channel %s", dump_channel_id)
        try:
            sent = await bot_client.send_video(
                chat_id=dump_channel_id,
                video=upload_path,
                caption=caption,
                supports_streaming=True,
            )
            media = getattr(sent, "video", None) or getattr(sent, "document", None)
            file_id = getattr(media, "file_id", None)
            return sent.id, file_id
        except Exception as e:
            logger.error("Upload failed: %s", e)
            return None, None
