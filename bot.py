from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from downloader import (
    DownloadError,
    download_video,
    inspect_media,
    validate_media_url,
    validate_remote_asset_url,
)


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# Telegram's normal public Bot API currently accepts newly uploaded videos and
# documents up to 50 MB. Keep a small safety margin.
MAX_TELEGRAM_FILE_SIZE = 49 * 1024 * 1024
JOB_TTL_SECONDS = 20 * 60
URL_PATTERN = re.compile(r"https://[^\s<>\"']+")

# Prevent several downloads from consuming all CPU, RAM, disk, or bandwidth.
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class MediaJob:
    user_id: int
    url: str
    info: dict
    expires_at: float


MEDIA_JOBS: dict[str, MediaJob] = {}


def extract_first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)

    if not match:
        return None

    return match.group(0).rstrip(".,;:!?)]}")


def create_title(info: dict) -> str:
    return str(info.get("title") or "Downloaded media")


def create_caption(info: dict, *, prefer_description: bool = False) -> str:
    if prefer_description:
        text = str(info.get("description") or info.get("title") or "")
        return text[:4000] or "No caption was returned for this media."

    title = create_title(info)
    uploader = str(
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or ""
    )

    lines = [title]

    if uploader and uploader.lower() != "none":
        lines.append(f"By: {uploader}")

    return "\n".join(lines)[:1000]


def create_info_text(info: dict) -> str:
    duration = info.get("duration")
    duration_text = "Unknown"

    if isinstance(duration, (int, float)):
        minutes, seconds = divmod(int(duration), 60)
        duration_text = f"{minutes}:{seconds:02d}"

    rows = [
        ("Title", create_title(info)),
        ("Uploader", info.get("uploader") or info.get("channel") or "Unknown"),
        ("Duration", duration_text),
        ("Provider", info.get("extractor_key") or info.get("extractor") or "Unknown"),
        ("Thumbnail", "Available" if info.get("thumbnail") else "Unavailable"),
    ]

    return "\n".join(f"{label}: {value}" for label, value in rows)[:4000]


def store_job(user_id: int, url: str, info: dict) -> str:
    now = time.time()
    expired_ids = [
        job_id
        for job_id, job in MEDIA_JOBS.items()
        if job.expires_at <= now
    ]

    for job_id in expired_ids:
        MEDIA_JOBS.pop(job_id, None)

    job_id = secrets.token_urlsafe(9)
    MEDIA_JOBS[job_id] = MediaJob(
        user_id=user_id,
        url=url,
        info=info,
        expires_at=now + JOB_TTL_SECONDS,
    )
    return job_id


def get_job(job_id: str, user_id: int) -> MediaJob:
    job = MEDIA_JOBS.get(job_id)

    if not job or job.expires_at <= time.time():
        MEDIA_JOBS.pop(job_id, None)
        raise DownloadError("This link expired. Please send it again.")

    if job.user_id != user_id:
        raise DownloadError("This download button belongs to another chat.")

    return job


def action_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Caption", callback_data=f"caption:{job_id}"),
                InlineKeyboardButton("Thumbnail", callback_data=f"thumbnail:{job_id}"),
            ],
            [
                InlineKeyboardButton("Info", callback_data=f"info:{job_id}"),
            ],
        ]
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Send me a public YouTube, Instagram, or TikTok link.\n\n"
        "Only download media you own or have permission to use. "
        "Private posts, paid content, DRM-protected media, playlists, "
        "and live streams are not supported."
    )


async def handle_media_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return

    submitted_url = extract_first_url(update.message.text)

    if not submitted_url:
        await update.message.reply_text(
            "Please send a complete HTTPS link from YouTube, Instagram, or TikTok."
        )
        return

    try:
        validated_url = await asyncio.to_thread(validate_media_url, submitted_url)
    except DownloadError as exc:
        await update.message.reply_text(str(exc))
        return

    status_message = await update.message.reply_text("Checking the media...")

    try:
        metadata = await asyncio.to_thread(inspect_media, validated_url)
        job_id = store_job(update.effective_user.id, validated_url, metadata)
        title = create_title(metadata)[:100]

        await status_message.edit_text(f"Downloading: {title}")
        await _send_video(
            update,
            context,
            MEDIA_JOBS[job_id],
            action_keyboard(job_id),
            status_message,
        )
    except DownloadError as exc:
        await status_message.edit_text(str(exc))
    except Exception:
        logger.exception("Unexpected metadata inspection error")
        await status_message.edit_text(
            "An unexpected error occurred while checking this link."
        )


async def _send_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    job: MediaJob,
    reply_markup: InlineKeyboardMarkup,
    status_message,
) -> None:
    if not update.effective_message:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_message.chat_id,
        action=ChatAction.UPLOAD_DOCUMENT,
    )

    async with DOWNLOAD_SEMAPHORE:
        with tempfile.TemporaryDirectory(prefix="media-bot-") as temporary_directory:
            media_path, final_info = await asyncio.to_thread(
                download_video,
                job.url,
                Path(temporary_directory),
            )

            file_size = media_path.stat().st_size

            if file_size > MAX_TELEGRAM_FILE_SIZE:
                size_mb = file_size / (1024 * 1024)
                await update.effective_message.reply_text(
                    f"The downloaded file is {size_mb:.1f} MB. "
                    "This MVP only sends files below 49 MB."
                )
                return

            caption = create_caption(final_info)
            await status_message.edit_text("Uploading to Telegram...")

            with media_path.open("rb") as media_file:
                suffix = media_path.suffix.lower()

                if suffix == ".mp4":
                    await update.effective_message.reply_video(
                        video=media_file,
                        caption=caption,
                        reply_markup=reply_markup,
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                    )
                else:
                    await update.effective_message.reply_document(
                        document=media_file,
                        caption=caption,
                        reply_markup=reply_markup,
                        read_timeout=120,
                        write_timeout=120,
                    )

            try:
                await status_message.delete()
            except TelegramError:
                pass


async def handle_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query or not update.effective_user:
        return

    await query.answer()

    try:
        if not query.data or ":" not in query.data:
            raise DownloadError("That action is not supported.")

        action, job_id = query.data.split(":", 1)
        job = get_job(job_id, update.effective_user.id)
        message = update.effective_message

        if not message:
            raise DownloadError("This button is no longer attached to a message.")

        if action == "caption":
            await message.reply_text(create_caption(job.info, prefer_description=True))
            return

        if action == "thumbnail":
            thumbnail_url = str(job.info.get("thumbnail") or "")

            if not thumbnail_url:
                raise DownloadError("No thumbnail was returned for this media.")

            await asyncio.to_thread(validate_remote_asset_url, thumbnail_url)
            await message.reply_photo(photo=thumbnail_url)
            return

        if action == "info":
            await message.reply_text(create_info_text(job.info))
            return

        raise DownloadError("That action is not supported.")

    except DownloadError as exc:
        if update.effective_message:
            await update.effective_message.reply_text(str(exc))
    except TelegramError:
        logger.exception("Telegram action failed")

        if update.effective_message:
            await update.effective_message.reply_text(
                "Telegram could not complete that action."
            )
    except Exception:
        logger.exception("Unexpected media action error")

        if update.effective_message:
            await update.effective_message.reply_text(
                "An unexpected error occurred while processing this action."
            )


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOTFATHER_TOKEN_HERE":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before starting the bot.")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_action))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_media_link,
        )
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
