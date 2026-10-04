import subprocess
import sys
from pathlib import Path
from mutagen.flac import FLAC


def split_flac_by_cue(cue_path, flac_path, output_dir=None):
    cue_path = Path(cue_path)
    flac_path = Path(flac_path)

    if not cue_path.exists() or not flac_path.exists():
        print(
            r"Error: Visualizing path issues? Make sure both .cue and .flac files exist."
        )
        sys.exit(1)

    if output_dir is None:
        output_dir = flac_path.parent / f"{flac_path.stem}_split"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"--- Processing: {flac_path.name} ---")
    print("Step 1: Splitting audio tracks losslessly...")

    # shnsplit command: %n=track number, %t=track title (temporary placeholder)
    # We output directly to FLAC format
    split_cmd = [
        "shnsplit",
        "-f",
        str(cue_path),
        "-o",
        "flac",
        "-d",
        str(output_dir),
        "-t",
        "%n",
        str(flac_path),
    ]

    try:
        subprocess.run(split_cmd, check=True, stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print(
            "Error: shnsplit failed. Ensure shntool and flac are installed correctly."
        )
        return

    print("Step 2: Parsing CUE sheet metadata to apply tags...")
    tracks_metadata = parse_cue(cue_path)

    print("Step 3: Tagging split files...")
    # shnsplit names files like 01.flac, 02.flac, etc.
    for track_num_str, meta in tracks_metadata.items():
        expected_filename = output_dir / f"{track_num_str}.flac"

        if expected_filename.exists():
            # Apply tags using mutagen
            audio = FLAC(expected_filename)
            audio["tracknumber"] = track_num_str
            audio["title"] = meta.get("title", f"Track {track_num_str}")
            if "artist" in meta:
                audio["artist"] = meta["artist"]
            if "album" in meta:
                audio["album"] = meta["album"]

            audio.save()

            # Rename the file to a cleaner '01 - Track Title.flac' format
            clean_title = "".join(
                c for c in meta.get("title", "") if c.isalnum() or c in "._- "
            ).strip()
            new_filename = output_dir / f"{track_num_str} - {clean_title}.flac"
            expected_filename.rename(new_filename)

    print(f"Success! Split tracks saved to: {output_dir}")


def parse_cue(cue_path):
    """A lightweight parser to extract Album, Artist, and Track info from the .cue file"""
    tracks = {}
    current_track = None
    album_artist = ""
    album_title = ""

    with open(cue_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if line.startswith("PERFORMER") and current_track is None:
                album_artist = line.split('"', 1)[1].rsplit('"', 1)[0]
            elif line.startswith("TITLE") and current_track is None:
                album_title = line.split('"', 1)[1].rsplit('"', 1)[0]
            elif line.startswith("TRACK"):
                current_track = line.split()[1]
                tracks[current_track] = {
                    "album": album_title,
                    "artist": album_artist,  # Default to album artist, overwritten if track has custom performer
                }
            elif current_track and line.startswith("TITLE"):
                tracks[current_track]["title"] = line.split('"', 1)[1].rsplit('"', 1)[0]
            elif current_track and line.startswith("PERFORMER"):
                tracks[current_track]["artist"] = line.split('"', 1)[1].rsplit('"', 1)[
                    0
                ]

    return tracks
