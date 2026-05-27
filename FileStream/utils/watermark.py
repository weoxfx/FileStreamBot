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
from pathlib import Path

logger = logging.getLogger(__name__)

WATERMARK_TEXT = "Tsukuyomi"
WATERMARK_FONT = "DejaVuSans-Bold"
WATERMARK_OPACITY = 0.45
WATERMARK_FONTSIZE = 36


def _build_ffmpeg_cmd(input_path: str, output_path: str) -> list[str]:
    drawtext = (
        f"drawtext=text='{WATERMARK_TEXT}'"
        f":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        f":fontsize={WATERMARK_FONTSIZE}"
        f":fontcolor=white@{WATERMARK_OPACITY}"
        f":x=w-tw-20"
        f":y=20"
        f":shadowcolor=black@0.3"
        f":shadowx=2"
        f":shadowy=2"
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
    logger.info("Running ffmpeg watermark: %s", " ".join(cmd))
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
        logger.error("ffmpeg not found — install it via nix")
        return False


async def apply_watermark_and_upload(
    bot_client,
    original_file_id: str,
    original_file_name: str,
    dump_channel_id: int,
    caption: str = "",
) -> tuple[int | None, str | None]:
    """
    Download the file, burn watermark, re-upload to dump_channel.
    Returns (message_id, new_file_id) or (None, None) on failure.
    Non-video files are forwarded as-is (no watermark needed).
    """
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw_" + original_file_name)
        wm_path = os.path.join(tmpdir, "wm_" + original_file_name)

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
        if not ok:
            logger.error("ffmpeg failed, uploading original without watermark")
            wm_path = dl_path

        logger.info("Uploading watermarked file to dump channel")
        try:
            sent = await bot_client.send_video(
                chat_id=dump_channel_id,
                video=wm_path,
                caption=caption,
                supports_streaming=True,
            )
            media = getattr(sent, "video", None) or getattr(sent, "document", None)
            file_id = getattr(media, "file_id", None)
            return sent.id, file_id
        except Exception as e:
            logger.error("Failed to upload watermarked video: %s", e)
            return None, None
