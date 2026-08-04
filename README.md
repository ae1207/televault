# Media Downloader Bot

A Telegram bot that validates public YouTube, Instagram, or TikTok links, extracts metadata with `yt-dlp`, automatically uploads the video, then lets the user copy the caption, fetch the thumbnail, or view basic media information.

Only use this for media you own, have permission to download, or that the platform explicitly permits downloading. Public availability is not the same as permission to copy or redistribute.

## Requirements

- Python 3.11 or newer
- FFmpeg
- A Telegram bot token from BotFather

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Install FFmpeg on Windows:

```powershell
winget install Gyan.FFmpeg
ffmpeg -version
```

Set your bot token in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=PASTE_YOUR_BOTFATHER_TOKEN_HERE
```

## Run

```powershell
python bot.py
```

Then send the bot a supported HTTPS link.

## Current Limits

- Public YouTube, Instagram, and TikTok links only.
- Playlists, multi-post downloads, live streams, private media, paid content, DRM-protected media, and login-only media are rejected.
- Videos longer than 15 minutes are rejected.
- Telegram uploads are capped at 49 MB for this MVP.
- Jobs are stored in memory for 20 minutes, so restarting the bot clears pending buttons.
- No user-supplied cookies are accepted.

## Production Notes

Before exposing the bot publicly, add per-user rate limiting, persistent job storage, queue workers, container-level CPU/memory/disk/time limits, outbound firewall restrictions, structured cleanup monitoring, and staging tests for each `yt-dlp` update.
