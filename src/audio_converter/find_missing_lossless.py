import csv
import re
from pathlib import Path

from .paths import (
    AIFF_ROOT,
    FLAC_ROOT,
    MISSING_REPORT_PATH,
    MP3_ROOT,
    NO_ALBUM_NAMES,
)

# Desktop export (kept as an alias so old references still work)
TXT_EXPORT_PATH = MISSING_REPORT_PATH


def normalize_rel_path(path: Path) -> str:
    """
    Normalizes relative path strings to ensure matches across folders
    even if extension differs (.mp3 vs .aiff vs .flac).
    """
    clean_stem = str(path.with_suffix("")).lower()
    return re.sub(r"[^\w/]", "", clean_stem)


def parse_folder_structure(rel_path: Path) -> tuple[str, str, str, str]:
    """
    Parses artist, album, year, and track title directly from folder/file path.
    Handles 'Various Artists/No Album/track.ext' as standalone tracks.
    """
    parts = rel_path.parts
    filename = rel_path.stem

    artist = "Unknown Artist"
    album = "Unknown Album"
    year = "N/A"

    if len(parts) >= 3:
        artist = parts[0]
        album_folder = parts[1]
    elif len(parts) == 2:
        album_folder = parts[0]
    else:
        album_folder = ""

    if album_folder:
        # Check if the folder name is explicitly a "No Album" place-holder
        if album_folder.lower().strip() in NO_ALBUM_NAMES:
            album = "No Album"
        else:
            # Check if album folder starts with a 4-digit year e.g. "1999 Club Hits"
            match = re.match(r"^(\d{4})\s+(.+)$", album_folder)
            if match:
                year = match.group(1)
                album = match.group(2)
            else:
                album = album_folder

    return artist, album, year, filename


def build_lossless_folder_index(aiff_root: Path, flac_root: Path) -> set:
    """Indexes relative folder paths for all AIFF and FLAC files."""
    index = set()

    if aiff_root.exists():
        for path in aiff_root.rglob("*.aiff"):
            try:
                rel = path.relative_to(aiff_root)
                index.add(normalize_rel_path(rel))
            except ValueError:
                pass

    if flac_root.exists():
        for path in flac_root.rglob("*.flac"):
            try:
                rel = path.relative_to(flac_root)
                index.add(normalize_rel_path(rel))
            except ValueError:
                pass

    return index


def export_txt(missing_albums: dict, standalone_tracks: list):
    """Writes formatted text report based on folder structure."""
    lines = []
    lines.append("=" * 60)
    lines.append("   FOLDER-BASED MISSING LOSSLESS REPORT (COMPILATION FRIENDLY)")
    lines.append("=" * 60 + "\n")

    if missing_albums:
        lines.append("--- MISSING ALBUM FOLDERS ---")
        for (artist, album, year), tracks in sorted(missing_albums.items()):
            year_str = f"({year}) " if year != "N/A" else ""
            artist_str = f"{artist} - " if artist != "Unknown Artist" else ""
            lines.append(
                f"• {artist_str}{year_str}{album} [{len(tracks)} tracks missing]"
            )
            for trk in sorted(tracks):
                lines.append(f"    - {trk}")
        lines.append("")

    if standalone_tracks:
        lines.append("--- STANDALONE MISSING TRACKS (NO ALBUM) ---")
        for artist, trk in sorted(standalone_tracks):
            artist_prefix = f"{artist} - " if artist != "Unknown Artist" else ""
            lines.append(f"• {artist_prefix}{trk}")
        lines.append("")

    total_albums = len(missing_albums)
    total_loose = len(standalone_tracks)
    lines.append(
        f"Summary: {total_albums} album folders missing, {total_loose} loose tracks missing."
    )

    TXT_EXPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def find_missing_lossless():
    print("Indexing folder paths for AIFF and FLAC libraries...")
    lossless_index = build_lossless_folder_index(AIFF_ROOT, FLAC_ROOT)

    print("Scanning MP3 folder paths...")
    mp3_files = list(MP3_ROOT.rglob("*.mp3"))

    missing_albums = {}  # (artist, album, year): list of missing track filenames
    standalone_tracks = []  # tuple of (artist, track_name)

    for mp3_file in mp3_files:
        rel_path = mp3_file.relative_to(MP3_ROOT)
        norm_key = normalize_rel_path(rel_path)

        # Skip if folder relative path matches in AIFF or FLAC
        if norm_key in lossless_index:
            continue

        # File is missing in lossless
        artist, album, year, track_name = parse_folder_structure(rel_path)

        if album not in ("Unknown Album", "No Album"):
            album_key = (artist, album, year)
            if album_key not in missing_albums:
                missing_albums[album_key] = []
            missing_albums[album_key].append(track_name)
        else:
            standalone_tracks.append((artist, track_name))

    # Export to Desktop
    export_txt(missing_albums, standalone_tracks)

    print("\nScan complete!")
    print(f"• Text Report: {TXT_EXPORT_PATH}")


if __name__ == "__main__":
    find_missing_lossless()
