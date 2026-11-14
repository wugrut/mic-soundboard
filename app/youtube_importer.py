"""Download audio tracks from YouTube / YouTube Music URLs (single or playlist) into sounds/."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import yt_dlp


def _sanitize_filename(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].strip()
    return cleaned or "youtube_track"


def import_youtube_audio(
    url: str, sounds_dir: Path, max_playlist_items: int = 10
) -> List[Tuple[Path, str]]:
    """Download best audio stream(s) for a YouTube / YT Music track or playlist.

    Returns a list of (downloaded_file_path, track_title).
    """
    url = url.strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Please enter a valid http(s) YouTube or YouTube Music URL")

    sounds_dir.mkdir(parents=True, exist_ok=True)
    is_playlist_url = "playlist?" in url.lower()

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": not is_playlist_url,
        "playlistend": max_playlist_items,
        "ignoreerrors": "only_download",
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
        "outtmpl": str(sounds_dir / "%(title).80s.%(ext)s"),
        "overwrites": False,
    }

    results: List[Tuple[Path, str]] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as exc:
            msg = str(exc)
            if "playlist does not exist" in msg.lower():
                raise ValueError(
                    "Playlist is Private or unavailable. Set it to Unlisted/Public, or paste a single song link."
                ) from exc
            raise ValueError(msg) from exc
        if not info:
            raise ValueError("Could not retrieve audio from that YouTube URL")

        entries = info.get("entries") if "entries" in info else [info]
        for entry in entries or []:
            if not entry:
                continue
            title = str(entry.get("title") or "youtube_track")
            # yt-dlp prepare_filename uses actual downloaded extension
            expected = Path(ydl.prepare_filename(entry))
            if expected.exists():
                results.append((expected, title))
            else:
                safe = _sanitize_filename(title)
                matches = sorted(sounds_dir.glob(f"{safe}.*"))
                if matches:
                    results.append((matches[0], title))

    if not results:
        raise FileNotFoundError("No audio tracks could be downloaded from that URL")

    return results
