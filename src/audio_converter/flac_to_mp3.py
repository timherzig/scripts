import subprocess
import shutil
from pathlib import Path

FLAC_ROOT = Path("/Volumes/Crucial1TB/Music/Music Flac")
MP3_ROOT = Path("/Volumes/Crucial1TB/Music/Music MP3")

MP3_BITRATE = "320k"  # or switch to V0 below

COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg")


def find_cover(album_dir: Path) -> Path | None:
    for name in COVER_NAMES:
        cover = album_dir / name
        if cover.exists():
            return cover
    return None


def needs_update(flac: Path, mp3: Path, cover: Path | None) -> bool:
    if not mp3.exists():
        return True

    mp3_time = mp3.stat().st_mtime
    if flac.stat().st_mtime > mp3_time:
        return True

    if cover and cover.stat().st_mtime > mp3_time:
        return True

    return False


def convert_flac_to_mp3(flac: Path, mp3: Path, cover: Path | None):
    mp3.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(flac),
    ]

    if cover:
        cmd += ["-i", str(cover)]

    cmd += [
        "-map_metadata",
        "0",
        "-map",
        "0:a",
    ]

    if cover:
        cmd += [
            "-map",
            "1:v",
            "-c:v",
            "mjpeg",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ]

    cmd += [
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        "-id3v2_version",
        "3",
        str(mp3),
    ]

    subprocess.run(cmd, check=True)


def copy_cover(cover: Path, mp3_album_dir: Path):
    dst = mp3_album_dir / cover.name
    if not dst.exists() or cover.stat().st_mtime > dst.stat().st_mtime:
        shutil.copy2(cover, dst)


def sync_library():
    for flac_file in FLAC_ROOT.rglob("*.flac"):
        rel = flac_file.relative_to(FLAC_ROOT)
        mp3_file = MP3_ROOT / rel.with_suffix(".mp3")

        album_dir = flac_file.parent
        cover = find_cover(album_dir)

        if needs_update(flac_file, mp3_file, cover):
            print(f"Converting: {rel}")
            convert_flac_to_mp3(flac_file, mp3_file, cover)

        if cover:
            copy_cover(cover, mp3_file.parent)
