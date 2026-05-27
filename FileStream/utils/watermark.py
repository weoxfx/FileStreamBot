"""
ffmpeg watermark pipeline.
Downloads a video from Telegram, burns in the "Tsukuyomi" watermark (top-right,
low opacity, clean font), then re-uploads to the dump channel.
Runs in a thread pool so it doesn't block the event loop.
"""
import os
import asyncio
import logging
import tempfile
import subprocess
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

WATERMARK_TEXT = "Tsukuyomi"
WATERMARK_OPACITY = 0.45
WATERMARK_FONTSIZE = 36


def _build_ffmpeg_cmd(input_path: str, output_path: str):
    drawtext = (
        "drawtext=text='Tsukuyomi'"
        ":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        ":fontsize=36"
        ":fontcolor=white@0.45"
        ":x=w-tw-20"
        ":y=20"
        ":shadowcolor=black@0.3"
        ":shadowx=2"
        ":shadowy=2"
    )
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", drawtext,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path
    ]


def _run_ffmpeg(input_path: str, output_path: str) -> bool:
    cmd = _build_ffmpeg_cmd(input_path, output_path)
    logger.info("Running ffmpeg watermark on: %s", input_path)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3600
        )
        if result.returncode != 0:
            logger.error("ffmpeg error:\n%s", result.stderr.decode(errors="replace"))
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out after 3600s")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found — install it")
        return False


async def apply_watermark_and_upload(
    bot_client,
    original_file_id: str,
    original_file_name: str,
    dump_channel_id: int,
    caption: str = "",
) -> Tuple[Optional[int], Optional[str]]:
    """
    Download the file, burn watermark, re-upload to dump_channel.
    Returns (message_id, new_file_id) or (None, None) on failure.
    """
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        safe_name = original_file_name.replace("/", "_").replace("\\", "_")
        raw_path = os.path.join(tmpdir, "raw_" + safe_name)
        wm_path = os.path.join(tmpdir, "wm_" + safe_name)

        logger.info("Downloading file for watermark: %s", original_file_name)
        try:
            dl_path = await bot_client.download_media(
                original_file_id,
                file_name=raw_path
            )
        except Exception as e:
            logger.error("Failed to download media: %s", e)
            return None, None

        if not dl_path or not os.path.exists(dl_path):
            logger.error("Download returned no file")
            return None, None

        ok = await loop.run_in_executor(None, _run_ffmpeg, dl_path, wm_path)
        upload_path = wm_path if ok and os.path.exists(wm_path) else dl_path

        logger.info("Uploading watermarked file to dump channel")
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
            logger.error("Failed to upload watermarked video: %s", e)
            return None, None
