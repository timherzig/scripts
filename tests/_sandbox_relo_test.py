"""Focused fixture for relocation strategies beyond the exact-path mirror.

Creates a tiny three-tree library with controlled durations, runs
relocate_rekordbox against a crafted XML, and prints the matching stats plus a
per-track report of the output XML.

Expected mapping of the crafted XML tracks:

* ``01 - Long.mp3``            -> "exact path"     (aiff mirror exists)
* ``02 - Short.mp3`` (120s)    -> unmatched        (fuzzy twin is the 40s
                                                     "Short Rmx" - same_audio
                                                     rejects the relocation)
* ``03 Long Variant.mp3``      -> "normalized match" (file missing -> duration
                                                     check skipped optimistically;
                                                     flac key bridges the spacing)
"""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from audio_converter.relocate_rekordbox import (
    path_to_uri,
    relocate_rekordbox_xml,
)

ROOT = Path(os.environ["MUSIC_ROOT"])
ALBUM = "VA/1999 Test Album"
for tree in ("flac", "mp3", "aiff"):
    (ROOT / tree / ALBUM).mkdir(parents=True, exist_ok=True)


def synth(dst: Path, seconds: int, fmt: str) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"anullsrc=r=44100:cl=mono", "-t", str(seconds)]
    if fmt == "flac":
        cmd += ["-c:a", "flac"]
    elif fmt == "aiff":
        cmd += ["-c:a", "pcm_s16be"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    cmd += [str(dst)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


synth(ROOT / "flac" / ALBUM / "01 - Long.flac", 120, "flac")
synth(ROOT / "flac" / ALBUM / "02 - Short Rmx.flac", 40, "flac")
synth(ROOT / "flac" / ALBUM / "03 - Long Variant.flac", 120, "flac")
synth(ROOT / "aiff" / ALBUM / "01 - Long.aiff", 120, "aiff")
synth(ROOT / "aiff" / ALBUM / "02 - Short Rmx.aiff", 40, "aiff")
synth(ROOT / "aiff" / ALBUM / "03 - Long Variant.aiff", 120, "aiff")
synth(ROOT / "mp3" / ALBUM / "01 - Long.mp3", 120, "mp3")
synth(ROOT / "mp3" / ALBUM / "02 - Short.mp3", 120, "mp3")

xml_in = ROOT / "relo_test.xml"
root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
collection = ET.SubElement(root, "COLLECTION")

tracks = [
    ("exact", "01 - Long.mp3", "01 - Long.mp3", "exact path"),
    ("reject", "02 - Short.mp3", "02 - Short.mp3", "duration mismatch"),
    ("optimistic", "03 Long Variant.mp3", "03 Long Variant.mp3", "missing original"),
]
for i, (tag, name, file_name, _) in enumerate(tracks, start=1):
    ET.SubElement(
        collection,
        "TRACK",
        TrackID=str(i),
        Name=name,
        Artist="VA",
        Kind="MP3 File",
        Location=path_to_uri(ROOT / "mp3" / ALBUM / file_name),
    )
ET.ElementTree(root).write(xml_in, encoding="utf-8", xml_declaration=True)

out = ROOT / "relo_test_aiff.xml"
unmatched = ROOT / "relo_test_unmatched.txt"
stats = relocate_rekordbox_xml(xml_in=xml_in, xml_out=out, unmatched_path=unmatched)

# --- per-track verification ------------------------------------------------
tree = ET.parse(out)
root = tree.getroot()
collection = root.find("COLLECTION")
labels = {1: "exact", 2: "reject", 3: "optimistic"}
print("\n--- output tracks ---")
for track in collection.findall("TRACK"):
    tid = int(track.get("TrackID"))
    loc = track.get("Location")
    print(f"TrackID {tid:>2} [{labels[tid]:>9}] -> {loc}")

if unmatched.exists():
    print("\n--- unmatched report ---")
    print(unmatched.read_text().strip())

print("\nstats:", stats)