"""Shared library locations and small filesystem helpers.

Every module imports its roots from here so the three trees (flac / mp3 / aiff)
can never drift apart. Set the ``MUSIC_ROOT`` environment variable to point the
whole tool suite at a different directory (used for sandbox testing).
"""

from __future__ import annotations

import os
from pathlib import Path

MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", "/Volumes/nas/media/music"))

FLAC_ROOT = MUSIC_ROOT / "flac"
MP3_ROOT = MUSIC_ROOT / "mp3"
AIFF_ROOT = MUSIC_ROOT / "aiff"

# Bookkeeping files that live next to the three trees
MANIFEST_PATH = MUSIC_ROOT / "lossy_sourced_manifest.json"
MB_CACHE_PATH = MUSIC_ROOT / ".musicbrainz_cache.json"
MISSING_REPORT_PATH = MUSIC_ROOT / "missing_lossless_report.txt"
RENAME_REPORT_PATH = MUSIC_ROOT / "rename_report.txt"
RENAME_HISTORY_PATH = MUSIC_ROOT / "rename_history.txt"

# Rekordbox XML export (override with REKORDBOX_XML / REKORDBOX_XML_OUT)
REKORDBOX_XML_IN = Path(
    os.environ.get("REKORDBOX_XML", "~/Desktop/rekordbox.xml")
).expanduser()
REKORDBOX_XML_OUT = Path(
    os.environ.get("REKORDBOX_XML_OUT", "~/Desktop/rekordbox_aiff.xml")
).expanduser()
REKORDBOX_UNMATCHED_PATH = Path(
    os.environ.get("REKORDBOX_UNMATCHED", "~/Desktop/rekordbox_unmatched.txt")
).expanduser()

ROOTS = {"flac": FLAC_ROOT, "mp3": MP3_ROOT, "aiff": AIFF_ROOT}
EXTS = {"flac": ".flac", "mp3": ".mp3", "aiff": ".aiff"}

COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg")
MP3_BITRATE = "320k"

# Folder names that stand for "no album here" (loose tracks / singles)
NO_ALBUM_NAMES = {
    "no album",
    "unknown album",
    "no_album",
    "unknown_album",
    "loose tracks",
    "singles",
    "youtube download",
    "misc",
    "downloads",
    "singles & ep",
    "singles and eps",
}

# Characters that are illegal on FAT32/exFAT (CDJ USB sticks)
FAT_INVALID = set('<>:"|?*')

# Result of a conversion: metadata is written by ffmpeg from the source tags
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def find_cover(album_dir: Path) -> Path | None:
    """Return the album cover image inside *album_dir*, if any."""
    for name in COVER_NAMES:
        cover = album_dir / name
        if cover.is_file():
            return cover
    return None


def read_cover(album_dir: Path) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, mime_type)`` for the album cover, if any."""
    cover = find_cover(album_dir)
    if cover is None:
        return None
    mime = IMAGE_MIME.get(cover.suffix.lower(), "image/jpeg")
    return cover.read_bytes(), mime


def prune_empty_dirs(root: Path, start: Path | None = None) -> int:
    """Remove empty directories bottom-up inside *root* (never removes *root*).

    Directories still containing files (e.g. ``.DS_Store``) are kept.
    Returns the number of directories removed.
    """
    removed = 0
    if not root.exists():
        return 0
    # Collect the subtree we are allowed to touch
    base = start if start is not None else root
    if not base.exists():
        return 0
    dirs = sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True)
    for directory in dirs:
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                removed += 1
        except OSError:
            continue
    return removed
