import subprocess
import shutil
import os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm

from .paths import AIFF_ROOT, FLAC_ROOT, MP3_ROOT, MP3_BITRATE, find_cover


def needs_update(src: Path, dst: Path, cover: Path | None) -> bool:
    if not dst.exists():
        return True

    dst_mtime = dst.stat().st_mtime
    if src.stat().st_mtime > dst_mtime:
        return True

    return bool(cover and cover.stat().st_mtime > dst_mtime)


def convert_flac_to_mp3(flac: Path, mp3: Path, cover: Path | None):
    mp3.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-i", str(flac)]

    if cover:
        cmd += ["-i", str(cover)]

    cmd += ["-map_metadata", "0", "-map", "0:a"]

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

    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def convert_flac_to_aiff(flac: Path, aiff: Path, cover: Path | None):
    aiff.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-i", str(flac)]

    if cover:
        cmd += ["-i", str(cover)]

    cmd += ["-map_metadata", "0", "-map", "0:a"]

    if cover:
        cmd += [
            "-map",
            "1:v",
            "-c:v",
            "copy",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ]

    # Universal 16-bit 44.1kHz AIFF + Triangular Dithering for CDJ-850/900 compatibility
    cmd += [
        "-af",
        "aresample=out_sample_rate=44100:dither_method=triangular",
        "-c:a",
        "pcm_s16be",
        "-write_id3v2",
        "1",
        "-id3v2_version",
        "3",
        str(aiff),
    ]

    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def copy_cover(cover: Path, target_album_dir: Path):
    dst = target_album_dir / cover.name
    if not dst.exists() or cover.stat().st_mtime > dst.stat().st_mtime:
        shutil.copy2(cover, dst)


def process_file_task(task: tuple[str, Path, Path, Path | None]) -> str:
    """Worker task performed in parallel."""
    fmt, flac_file, target_file, cover = task

    if fmt == "MP3":
        convert_flac_to_mp3(flac_file, target_file, cover)
    elif fmt == "AIFF":
        convert_flac_to_aiff(flac_file, target_file, cover)

    if cover:
        copy_cover(cover, target_file.parent)

    return f"[{fmt}] {flac_file.name}"


def sync_library():
    print("Scanning FLAC files...")
    flac_files = list(FLAC_ROOT.rglob("*.flac"))

    tasks = []

    # Queue conversions that need processing
    for flac_file in flac_files:
        rel = flac_file.relative_to(FLAC_ROOT)
        album_dir = flac_file.parent
        cover = find_cover(album_dir)

        # Check MP3
        mp3_file = MP3_ROOT / rel.with_suffix(".mp3")
        if needs_update(flac_file, mp3_file, cover):
            tasks.append(("MP3", flac_file, mp3_file, cover))

        # Check AIFF
        aiff_file = AIFF_ROOT / rel.with_suffix(".aiff")
        if needs_update(flac_file, aiff_file, cover):
            tasks.append(("AIFF", flac_file, aiff_file, cover))

    if not tasks:
        print("Library is completely up to date!")
        return

    print(f"Found {len(tasks)} files to convert across MP3 and AIFF trees.")

    # Use max CPU threads available
    max_workers = os.cpu_count() or 4

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_file_task, task) for task in tasks]

        # Live progress bar updated on completed tasks
        with tqdm(total=len(futures), desc="Converting", unit="file") as pbar:
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    pbar.write(f"Error converting track: {e}")
                pbar.update(1)


if __name__ == "__main__":
    sync_library()