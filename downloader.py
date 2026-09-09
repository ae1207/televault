from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp


MAX_DURATION_SECONDS = 15 * 60

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
)

ALLOWED_EXTRACTORS = ("youtube", "instagram", "tiktok")


class DownloadError(Exception):
    """A user-safe media download error."""


def _domain_is_allowed(hostname: str) -> bool:
    hostname = hostname.lower().rstrip(".")

    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ALLOWED_DOMAINS
    )


def _validate_public_https_url(url: str, *, restrict_domains: bool) -> str:
    url = url.strip()

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise DownloadError("That URL is invalid.") from exc

    if parsed.scheme != "https":
        raise DownloadError("Only HTTPS links are supported.")

    if parsed.username or parsed.password:
        raise DownloadError("URLs containing credentials are not accepted.")

    hostname = parsed.hostname

    if not hostname:
        raise DownloadError("That URL is invalid.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("That URL is invalid.") from exc

    if port not in (None, 443):
        raise DownloadError("Only standard HTTPS links are supported.")

    if restrict_domains and not _domain_is_allowed(hostname):
        raise DownloadError(
            "Supported platforms are YouTube, Instagram, and TikTok."
        )

    try:
        addresses = socket.getaddrinfo(
            hostname,
            port or 443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise DownloadError("The address could not be resolved.") from exc

    for address in addresses:
        raw_ip = address[4][0]

        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue

        if not ip.is_global:
            raise DownloadError("Private or local network addresses are blocked.")

    return url


def validate_media_url(url: str) -> str:
    """
    Validate the submitted URL and reject obvious SSRF targets.

    Production deployments should also enforce outbound firewall rules,
    because application-level validation alone cannot stop every redirect
    or DNS-rebinding scenario.
    """
    return _validate_public_https_url(url, restrict_domains=True)


def validate_remote_asset_url(url: str) -> str:
    """
    Validate extractor-returned remote assets such as thumbnails.

    CDN hostnames differ by platform, so this check allows any public HTTPS host
    while still blocking credentials, local addresses, and private networks.
    """
    return _validate_public_https_url(url, restrict_domains=False)


def _extractor_is_allowed(info: dict) -> bool:
    extractor = str(
        info.get("extractor_key")
        or info.get("extractor")
        or ""
    ).lower()

    return extractor.startswith(ALLOWED_EXTRACTORS)


def inspect_media(url: str) -> dict:
    """Extract metadata without downloading the media."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(
            "The post could not be read. It may be private, deleted, "
            "region-restricted, login-only, or temporarily unsupported."
        ) from exc

    if not info:
        raise DownloadError("No media information was returned.")

    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise DownloadError("Playlists and multi-post downloads are disabled.")

    if info.get("is_live") or info.get("live_status") == "is_live":
        raise DownloadError("Live streams are not supported.")

    duration = info.get("duration")

    if duration and duration > MAX_DURATION_SECONDS:
        raise DownloadError(
            f"Videos longer than {MAX_DURATION_SECONDS // 60} minutes "
            "are not supported."
        )

    if not _extractor_is_allowed(info):
        raise DownloadError("The detected media provider is not supported.")

    return info


def _find_downloaded_media(output_directory: Path) -> Path:
    ignored_suffixes = {
        ".json",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".part",
        ".ytdl",
        ".description",
    }

    candidates = [
        path
        for path in output_directory.iterdir()
        if path.is_file() and path.suffix.lower() not in ignored_suffixes
    ]

    if not candidates:
        raise DownloadError("The download completed but no media file was produced.")

    return max(candidates, key=lambda path: path.stat().st_size)


def download_video(url: str, output_directory: Path) -> tuple[Path, dict]:
    """Download one Telegram-friendly video into the temporary directory."""
    output_directory.mkdir(parents=True, exist_ok=True)
    output_template = str(output_directory / "%(id)s.%(ext)s")

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": False,
        "nopart": True,
        "overwrites": True,
        "restrictfilenames": True,
        "outtmpl": output_template,
        "format": (
            "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/"
            "b[height<=720][ext=mp4]/"
            "best[height<=720]/best"
        ),
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            clean_info = ydl.sanitize_info(info)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(
            "The media download failed. The platform may have changed "
            "its delivery format or restricted this post."
        ) from exc

    return _find_downloaded_media(output_directory), clean_info
