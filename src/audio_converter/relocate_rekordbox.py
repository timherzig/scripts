"""Migrate a Rekordbox XML collection from MP3 paths to the AIFF tree.

Matching strategies, in order:

0. **Rename history** - if ``rename_history.txt`` (written by
   ``normalize_names``) contains the track's path, translate it through every
   recorded generation to the current tree and use the AIFF mirror directly.
   This covers *all* files renamed by any previous run (incl. disc-folder
   flattening, VA ``NN - Artist - Title`` rewrites, and loose-track naming
   changes) - deterministically, with no name guessing.
1. **Exact path mirror** - the MP3's relative path exists as an AIFF.
2. **Normalized mirror** - match the MP3 against the FLAC tree by normalized
   key (bridges naming differences), then use that file's relative path in the
   AIFF tree. Includes a fuzzy fallback for near-identical names.
3. **Tag lookup** - ``artist + title`` read from the AIFF's ID3 tags, for
   tracks that live outside the MP3 root.

Track attributes are refreshed from the target AIFF's tags (Name, Artist,
Album, Genre, Year) while ``TrackID`` - and therefore playlists - are kept
untouched. Tracks that cannot be matched are written to a report instead of
being silently dropped.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .matching import (
    build_album_index,
    build_file_index,
    fuzzy_match,
    normalize_rel_path,
    normalize_name,
    same_audio,
)
from .paths import (
    AIFF_ROOT,
    FLAC_ROOT,
    MP3_ROOT,
    RENAME_HISTORY_PATH,
    REKORDBOX_UNMATCHED_PATH,
    REKORDBOX_XML_IN,
    REKORDBOX_XML_OUT,
)
from .tags import read_tags


def uri_to_path(uri: str) -> Path:
    """Convert a file:// URI from a Rekordbox XML into a local path."""
    parsed = urlparse(uri)
    if parsed.netloc and parsed.netloc not in ("localhost", "127.0.0.1"):
        # remote/share style location - keep the path part only
        pass
    return Path(unquote(parsed.path))


def path_to_uri(path: Path) -> str:
    """Convert a local path into the file://localhost URI style Rekordbox uses."""
    posix_path = path.as_posix()
    if not posix_path.startswith("/"):
        posix_path = "/" + posix_path
    return "file://localhost" + quote(posix_path)


def sanitize(text: str) -> str:
    if not text:
        return ""
    return normalize_name(text)


def _build_tag_lookup(aiff_root: Path) -> dict[str, Path]:
    """artist_title -> AIFF path (first occurrence wins)."""
    lookup: dict[str, Path] = {}
    for aiff_file in sorted(aiff_root.rglob("*.aiff")):
        tags = read_tags(aiff_file)
        artist = tags.get("artist", "")
        title = tags.get("title", "")
        if not (artist and title):
            continue
        key = f"{sanitize(artist)}_{sanitize(title)}"
        lookup.setdefault(key, aiff_file)
    return lookup


def load_rename_history(path: Path) -> dict[str, str]:
    """Map old absolute path -> new absolute path from ``normalize_names``.

    ``rename_history.txt`` accumulates every rename across runs, so a path
    from any earlier generation can be chained to its current location.
    """
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    for line in path.read_text(encoding="utf-8").splitlines():
        src, sep, dst = line.partition(" -> ")
        if sep and src.strip() and dst.strip():
            mapping[src.strip()] = dst.strip()
    return mapping


def translate_path(path: Path, history: dict[str, str]) -> Path:
    """Follow the rename history until the path is stable (no-op if unknown)."""
    cur = path
    seen: set[str] = set()
    while str(cur) in history and str(cur) not in seen:
        seen.add(str(cur))
        cur = Path(history[str(cur)])
    return cur


def _find_target(
    original: Path,
    flac_flat: dict,
    flac_by_album: dict,
    aiff_flat: dict,
    aiff_by_album: dict,
) -> tuple[Path | None, str]:
    # 1. exact mirror of the mp3 path inside the aiff tree
    try:
        rel = original.relative_to(MP3_ROOT)
    except ValueError:
        rel = None

    if rel is not None:
        candidate = AIFF_ROOT / rel.with_suffix(".aiff")
        if candidate.exists():
            return candidate, "exact path"

        # 2. normalized match through the flac tree (handles renames)
        key = normalize_rel_path(rel)
        flac_twin = flac_flat.get(key)
        if flac_twin is None:
            flac_twin = fuzzy_match(key, flac_by_album)
            # a fuzzy flac twin must hold the same recording, otherwise we
            # would point the track at a different song
            if flac_twin is not None and not same_audio(original, flac_twin):
                flac_twin = None
        if flac_twin is not None:
            candidate = AIFF_ROOT / flac_twin.relative_to(FLAC_ROOT).with_suffix(".aiff")
            if candidate.exists() and same_audio(original, candidate):
                return candidate, "normalized match"

        # 2b. fuzzy directly against the aiff tree
        candidate = fuzzy_match(key, aiff_by_album)
        if candidate is not None and same_audio(original, candidate):
            return candidate, "fuzzy match"

    return None, "unmatched"


# --------------------------------------------------------------------------- #
# task
# --------------------------------------------------------------------------- #
def relocate_rekordbox_xml(
    xml_in: str | Path | None = None,
    xml_out: str | Path | None = None,
    unmatched_path: str | Path | None = None,
) -> dict:
    xml_in = Path(xml_in).expanduser() if xml_in else REKORDBOX_XML_IN
    xml_out = Path(xml_out).expanduser() if xml_out else REKORDBOX_XML_OUT
    unmatched_path = (
        Path(unmatched_path).expanduser() if unmatched_path else REKORDBOX_UNMATCHED_PATH
    )

    if not xml_in.exists():
        print(
            f"Rekordbox XML not found: {xml_in}\n"
            "Export it from rekordbox first: File -> Export -> Collection "
            "('rekordbox XML format')."
        )
        return {"error": f"missing {xml_in}"}

    print(f"Parsing {xml_in} ...")
    tree = ET.parse(xml_in)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        print("Invalid Rekordbox XML structure (no <COLLECTION> element).")
        return {"error": "invalid xml"}

    print("Indexing FLAC and AIFF trees...")
    flac_flat = build_file_index(FLAC_ROOT, "*.flac")
    aiff_flat = build_file_index(AIFF_ROOT, "*.aiff")
    flac_by_album = build_album_index(flac_flat)
    aiff_by_album = build_album_index(aiff_flat)

    tracks = collection.findall("TRACK")
    print(f"Processing {len(tracks)} tracks...")

    # Tag lookup is only needed for tracks outside the MP3 root; build it lazily
    tag_lookup: dict[str, Path] | None = None

    history = load_rename_history(RENAME_HISTORY_PATH)
    if history:
        print(f"  Applying rename history ({len(history)} entries)...")

    stats = {"relocated": 0, "already_aiff": 0, "unmatched": 0}
    strategies: dict[str, int] = {}
    unmatched: list[str] = []

    for track in tracks:
        location = track.get("Location")
        if not location:
            stats["unmatched"] += 1
            unmatched.append(f"{track.get('Name', '?')} - {track.get('Artist', '?')}: no Location")
            continue

        original = uri_to_path(location)

        # already this generation's aiff: keep as-is
        if original.suffix.lower() in {".aiff", ".aif"} and original.exists():
            stats["already_aiff"] += 1
            continue

        artist = track.get("Artist", "")
        title = track.get("Name", "")

        # 0. deterministic translation through normalize's rename history
        translated = translate_path(original, history) if history else original
        target: Path | None = None
        strategy = ""

        if translated.suffix.lower() in {".aiff", ".aif"} and translated.exists():
            # a renamed aiff (the export predates a normalize run): refresh path
            if translated != original:
                track.set("Location", path_to_uri(translated))
            stats["already_aiff"] += 1
            strategies["history refresh"] = strategies.get("history refresh", 0) + 1
            continue

        if translated != original:
            # the history names the current mp3: use its aiff mirror directly
            try:
                rel = translated.relative_to(MP3_ROOT)
            except ValueError:
                rel = None
            if rel is not None:
                cand = AIFF_ROOT / rel.with_suffix(".aiff")
                if cand.exists():
                    target, strategy = cand, "history"

        if target is None:
            target, strategy = _find_target(
                original, flac_flat, flac_by_album, aiff_flat, aiff_by_album
            )

        if target is None:
            # last resort: artist + title from the AIFF's own tags (built once)
            if tag_lookup is None:
                print("  Building AIFF tag lookup for fallback matching...")
                tag_lookup = _build_tag_lookup(AIFF_ROOT)
            key = f"{sanitize(artist)}_{sanitize(title)}"
            if key in tag_lookup:
                target = tag_lookup[key]
                strategy = "tag match"

        if target is None or not target.exists():
            stats["unmatched"] += 1
            strategies["unmatched"] = strategies.get("unmatched", 0) + 1
            unmatched.append(f"{title} - {artist}: {original}")
            continue

        # ---- relocate + refresh metadata from the AIFF's own tags ---------
        track.set("Location", path_to_uri(target))
        track.set("Kind", "AIFF file")
        track.set("BitRate", "1411")       # 16-bit / 44.1 kHz PCM
        track.set("SampleRate", "44100")

        tags = read_tags(target)
        for attr, field in (
            ("Name", "title"),
            ("Artist", "artist"),
            ("Album", "album"),
            ("Genre", "genre"),
        ):
            value = tags.get(field)
            if value and track.get(attr) != value:
                track.set(attr, value)
        if tags.get("date"):
            year = tags["date"][:4]
            if year and track.get("Year") != year:
                track.set("Year", year)

        stats["relocated"] += 1
        strategies[strategy] = strategies.get(strategy, 0) + 1

    xml_out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(xml_out, encoding="utf-8", xml_declaration=True)

    if unmatched:
        unmatched_path.parent.mkdir(parents=True, exist_ok=True)
        unmatched_path.write_text("\n".join(unmatched) + "\n", encoding="utf-8")

    print("\nDone!")
    print(f"  Relocated to AIFF:      {stats['relocated']}")
    print(f"  Already pointed to AIFF: {stats['already_aiff']}")
    print(f"  Unmatched:              {stats['unmatched']}")
    for strategy, count in sorted(strategies.items()):
        print(f"      {strategy}: {count}")
    print(f"  New XML: {xml_out}")
    if unmatched:
        print(f"  Unmatched report: {unmatched_path}")

    return stats


if __name__ == "__main__":
    relocate_rekordbox_xml()
