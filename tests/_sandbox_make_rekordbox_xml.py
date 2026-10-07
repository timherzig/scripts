"""Build a sandbox Rekordbox XML for testing relocate_rekordbox.

Mixes: real mp3 tracks (in the sandbox tree), one track already pointing at an
AIFF, one zombie (missing file), one outside-the-mp3-root track that can only
be found through the AIFF tag lookup.
"""
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from audio_converter.relocate_rekordbox import path_to_uri
from audio_converter.tags import read_tags

MUSIC = Path(os.environ["MUSIC_ROOT"])
XML = MUSIC / "test_rekordbox.xml"

root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
collection = ET.SubElement(root, "COLLECTION", Entries="0")

track_id = 1
mp3s = sorted((MUSIC / "mp3").rglob("*.mp3"))
for mp3 in mp3s:
    tags = read_tags(mp3)
    ET.SubElement(
        collection,
        "TRACK",
        TrackID=str(track_id),
        Name=tags.get("title") or mp3.stem,
        Artist=tags.get("artist") or "",
        Album=tags.get("album") or "",
        Genre=tags.get("genre") or "",
        Year=(tags.get("date") or "")[:4],
        Kind="MP3 File",
        BitRate="320",
        SampleRate="44100",
        Location=path_to_uri(mp3),
    )
    track_id += 1

# one track that already points at an AIFF
aiff_example = next((MUSIC / "aiff").rglob("*.aiff"))
tags = read_tags(aiff_example)
ET.SubElement(
    collection,
    "TRACK",
    TrackID=str(track_id),
    Name=tags.get("title") or aiff_example.stem,
    Artist=tags.get("artist") or "",
    Album=tags.get("album") or "",
    Kind="AIFF file",
    BitRate="1411",
    SampleRate="44100",
    Location=path_to_uri(aiff_example),
)
track_id += 1

# zombie: points at a file that does not exist
ET.SubElement(
    collection,
    "TRACK",
    TrackID=str(track_id),
    Name="Ghost Track",
    Artist="No One",
    Kind="MP3 File",
    Location=path_to_uri(MUSIC / "mp3/No One/1999 Ghost/99 - Ghost.mp3"),
)
track_id += 1

# outside the mp3 root -> must be resolved through the AIFF tag lookup
ET.SubElement(
    collection,
    "TRACK",
    TrackID=str(track_id),
    Name="The Genesis",
    Artist="Nas",
    Year="1994",
    Kind="MP3 File",
    Location="file://localhost/Users/Someone/Music/old%20drive/nas%201994/01%20-%20The%20Genesis.mp3",
)

tree = ET.ElementTree(root)
tree.write(XML, encoding="utf-8", xml_declaration=True)
print(f"Wrote {XML} with {track_id} tracks")