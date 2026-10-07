"""Scrape metadata - album/track tags and cover art - for the FLAC tree.

Sources:

* **MusicBrainz** - release search, track lists, artist credits, release dates.
* **Cover Art Archive** - official cover images (1200px preferred, 500px and
  full-size as fallbacks).

Only the FLAC tree is written; ``sync_tags`` pushes everything to MP3/AIFF
afterwards. Every lookup is cached in ``.musicbrainz_cache.json`` so re-runs
only pay the (1 request/second) MusicBrainz rate limit for new albums.

Policy:

* Album-level values (album, albumartist, date) always come from the matched
  MusicBrainz release.
* Track titles are only replaced when the existing title is missing or the two
  titles are near-identical (so odd editions keep their own names).
* Genres are never overwritten - MusicBrainz has no reliable genre data.
* Albums without a cover first harvest one from the MP3/AIFF trees (there are
  covers there that the FLAC tree is missing), then try the Cover Art Archive.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from .matching import build_album_index, build_file_index, fuzzy_match, normalize_rel_path, normalize_name, similarity
from .paths import FLAC_ROOT, MB_CACHE_PATH, MP3_ROOT, AIFF_ROOT, NO_ALBUM_NAMES, find_cover
from .tags import read_tags, update_tags

MB_WS = "https://musicbrainz.org/ws/2"
COVERART = "https://coverartarchive.org"
USER_AGENT = "audio-scripts/1.0 (local rekordbox/flac library tool)"
MB_DELAY = 1.1  # MusicBrainz terms: max 1 request/second

THRESHOLD = 0.55  # minimum release-match score
TITLE_THRESHOLD = 0.6  # replace an existing title only if it is at least this close

_last_mb_call = 0.0


# --------------------------------------------------------------------------- #
# HTTP with rate limiting
# --------------------------------------------------------------------------- #
def _throttle() -> None:
    global _last_mb_call
    wait = _last_mb_call + MB_DELAY - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_mb_call = time.monotonic()


def _http(url: str, params: dict | None = None, tries: int = 4) -> bytes | None:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    is_mb = "musicbrainz.org" in url
    for attempt in range(tries):
        if is_mb:
            _throttle()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (401, 403):
                return None
            if attempt == tries - 1:
                return None
            time.sleep(2**attempt)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == tries - 1:
                return None
            time.sleep(2**attempt)
    return None


def _http_json(url: str, params: dict | None = None) -> dict | list | None:
    data = _http(url, params)
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def load_cache() -> dict:
    if MB_CACHE_PATH.exists():
        try:
            cache = json.loads(MB_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cache, dict) and "releases" in cache:
                return cache
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "releases": {}, "recordings": {}}


def save_cache(cache: dict) -> None:
    tmp = MB_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MB_CACHE_PATH)


# --------------------------------------------------------------------------- #
# MusicBrainz queries
# --------------------------------------------------------------------------- #
def _escape(value: str) -> str:
    return value.replace("\\", " ").replace('"', "'")


def _credit(entry: dict | list | None) -> str:
    """Join an MB artist-credit (name + joinphrase) into one string."""
    if not entry:
        return ""
    if isinstance(entry, str):
        return entry
    parts = []
    for item in entry:
        if isinstance(item, dict):
            parts.append(str(item.get("name", "")))
            parts.append(str(item.get("joinphrase", "")))
    return "".join(parts).strip()


def _year_score(year: str, date: str) -> float:
    if not year:
        return 0.5
    if not date:
        return 0.5
    if date.startswith(year):
        return 1.0
    try:
        return 0.5 if abs(int(date[:4]) - int(year)) <= 1 else 0.0
    except (ValueError, IndexError):
        return 0.0


def score_release(release: dict, album: str, artist: str, year: str) -> float:
    score = (
        0.5 * similarity(album, release.get("title", ""))
        + 0.3 * (similarity(artist, _credit(release.get("artist-credit"))) if artist else 0.5)
        + 0.2 * _year_score(year, release.get("date") or "")
    )
    if str(release.get("status", "Official")).lower() != "official":
        score -= 0.1
    return score


def _tracks_of(release: dict) -> list[dict]:
    tracks = []
    for medium in release.get("media", []):
        disc = medium.get("position") or 1
        for track in medium.get("tracks", []):
            tracks.append(
                {
                    "disc": disc,
                    "pos": track.get("position"),
                    "title": track.get("title", ""),
                    "artist": _credit(track.get("artist-credit")),
                }
            )
    return tracks


DISC_FOLDER_RE = re.compile(r"^(?:cd|disc|dvd|part)\s*[-.]?\s*\d+$", re.IGNORECASE)


def album_for_search(album: str) -> str:
    """Drop a leading or trailing disc marker from an album title.

    ``"3 Feet High And Rising (CD 1)"`` -> ``"3 Feet High And Rising"``
    ``"CD1 - Greatest Hits"``          -> ``"Greatest Hits"``
    """
    cleaned = re.sub(
        r"^\s*(?:cd|disc|disk)\s*\d+(?:\s*[/-]\s*\d+)?\s*[-–—:]\s*",
        "",
        album,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*[-–—(]\s*(?:cd|disc|disk)\s*\d+(?:\s*(?:of|/|-)\s*\d+)?\s*\)?\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip() or album


def album_folder(album_dir: Path) -> Path:
    """Walk out of CD1/CD2 style sub-folders to the real album folder.

    ``.../1989 3 Feet High And Rising/CD1`` -> ``.../1989 3 Feet High And Rising``
    so both discs are treated as one release (and one cover).
    """
    folder = album_dir
    while DISC_FOLDER_RE.match(folder.name) and folder != FLAC_ROOT:
        folder = folder.parent
    return folder


def search_release(album: str, artist: str, year: str) -> dict | None:
    """Find the best matching MusicBrainz release, or None."""
    query = f'release:"{_escape(album)}"'
    if artist:
        query += f' AND artist:"{_escape(artist)}"'
    data = _http_json(
        f"{MB_WS}/release", {"query": query, "fmt": "json", "limit": 10}
    )
    releases = (data or {}).get("releases", []) if isinstance(data, dict) else []
    if not releases:
        return None

    best = max(releases, key=lambda rel: score_release(rel, album, artist, year))
    if score_release(best, album, artist, year) < THRESHOLD:
        return None

    detail = _http_json(
        f"{MB_WS}/release/{best['id']}",
        {"inc": "recordings+artist-credits+release-groups", "fmt": "json"},
    )
    if not isinstance(detail, dict):
        return None

    rg = detail.get("release-group") or {}
    return {
        "status": "matched",
        "mbid": detail["id"],
        "rg_mbid": rg.get("id"),
        "title": detail.get("title") or album,
        "date": detail.get("date") or "",
        "albumartist": _credit(detail.get("artist-credit")) or artist,
        "tracks": _tracks_of(detail),
        "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def search_recording(title: str, artist: str, year: str) -> dict | None:
    """Single-track lookup for loose files that have no album context."""
    query = f'recording:"{_escape(title)}"'
    if artist:
        query += f' AND artist:"{_escape(artist)}"'
    data = _http_json(
        f"{MB_WS}/recording", {"query": query, "fmt": "json", "limit": 5}
    )
    results = (data or {}).get("recordings", []) if isinstance(data, dict) else []
    if not results:
        return None

    def score(rec: dict) -> float:
        first_date = rec.get("first-release-date") or ""
        return (
            0.5 * similarity(title, rec.get("title", ""))
            + 0.3 * (similarity(artist, _credit(rec.get("artist-credit"))) if artist else 0.5)
            + 0.2 * _year_score(year, first_date)
        )

    best = max(results, key=score)
    if score(best) < THRESHOLD:
        return None
    releases = best.get("releases") or []
    return {
        "status": "matched",
        "mbid": best["id"],
        "rg_mbid": None,
        "title": best.get("title", ""),
        "date": best.get("first-release-date") or (releases[0].get("date", "") if releases else ""),
        "albumartist": _credit(best.get("artist-credit")) or artist,
        "release_mbid": releases[0]["id"] if releases else None,
        "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# cover art
# --------------------------------------------------------------------------- #
def fetch_cover(mbid: str | None, rg_mbid: str | None) -> tuple[bytes, str] | None:
    urls = []
    if mbid:
        urls += [
            f"{COVERART}/release/{mbid}/front-1200",
            f"{COVERART}/release/{mbid}/front-500",
            f"{COVERART}/release/{mbid}/front",
        ]
    if rg_mbid:
        urls += [
            f"{COVERART}/release-group/{rg_mbid}/front-1200",
            f"{COVERART}/release-group/{rg_mbid}/front-500",
            f"{COVERART}/release-group/{rg_mbid}/front",
        ]
    for url in urls:
        data = _http(url)
        if data and len(data) > 2000:
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data, "image/png"
            return data, "image/jpeg"
    return None


def _save_cover(album_dir: Path, data: bytes, mime: str) -> Path:
    name = "cover.png" if mime == "image/png" else "cover.jpg"
    path = album_dir / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _file_positions(path: Path) -> tuple[int, int]:
    """(disc, track) of a file from its tags, falling back to folder/file name."""
    tags = read_tags(path)
    disc = _number(tags.get("discnumber"))
    if disc is None:
        match = re.match(r"^(?:cd|disc|dvd|part)\s*[-.]?\s*(\d+)$", path.parent.name, re.I)
        disc = int(match.group(1)) if match else 1
    track = _number(tags.get("tracknumber")) or _number(path.stem)
    if track is None:
        track = 1
    return disc, track


def _group_identity(album_dir: Path, files: list[Path]) -> tuple[str, str, str, bool]:
    """Return (artist, album, year, has_album_context).

    *album_dir* is the album folder (see :func:`album_folder`). Files living
    directly under a bucket folder (``Misc/``, ``YouTube Download/``) or a
    "No Album" placeholder have no album context and get a per-track
    recording lookup instead.
    """
    tags = read_tags(files[0])
    artist = tags.get("albumartist") or tags.get("artist") or ""
    album = tags.get("album") or ""
    year = (tags.get("date") or "")[:4]

    rel = album_dir.relative_to(FLAC_ROOT)
    folder = album_dir.name
    at_root_level = len(rel.parts) == 1
    no_album_folder = folder.lower().strip() in NO_ALBUM_NAMES

    if at_root_level or no_album_folder:
        return artist, "", year, False

    if not album:
        # fall back to the folder name, minus a leading year ("1994 Illmatic")
        album = re.sub(r"^\d{4}\s+", "", folder).strip() or folder
    if not artist:
        artist = album_dir.parent.name
    return artist, album_for_search(album), year, True


def _find_twin(key: str, flat: dict, grouped: dict) -> Path | None:
    return flat.get(key) or fuzzy_match(key, grouped)


def _harvest_local_cover(
    album_dir: Path,
    first_file: Path,
    mp3_flat: dict,
    mp3_by_album: dict,
    aiff_flat: dict,
    aiff_by_album: dict,
) -> bool:
    """Copy a cover from the MP3/AIFF tree when the FLAC folder has none."""
    key = normalize_rel_path(first_file.relative_to(FLAC_ROOT))
    for flat, grouped in ((mp3_flat, mp3_by_album), (aiff_flat, aiff_by_album)):
        twin = _find_twin(key, flat, grouped)
        if twin is None:
            continue
        cover = find_cover(twin.parent)
        if cover is not None:
            _save_cover(
                album_dir,
                cover.read_bytes(),
                "image/png" if cover.suffix.lower() == ".png" else "image/jpeg",
            )
            return True
    return False


# --------------------------------------------------------------------------- #
# applying tags
# --------------------------------------------------------------------------- #
def _apply_release(album_dir: Path, files: list[Path], entry: dict) -> tuple[int, int]:
    """Write MB release data onto every FLAC in *album_dir*."""
    tracks = entry.get("tracks", [])
    by_pos = {(t["disc"], t["pos"]): t for t in tracks}
    by_title = {normalize_name(t["title"]): t for t in tracks if t.get("title")}

    changed_files = 0
    changed_fields = 0

    for path in files:
        tags = read_tags(path)
        values: dict[str, str] = {}

        disc, track = _file_positions(path)
        mb_track = by_pos.get((disc, track)) or by_pos.get((1, track))

        if mb_track is None:
            # fall back to a title match
            mb_track = by_title.get(normalize_name(tags.get("title") or path.stem))

        if mb_track and mb_track.get("title"):
            existing_title = tags.get("title") or ""
            if not existing_title or similarity(existing_title, mb_track["title"]) >= TITLE_THRESHOLD:
                values["title"] = mb_track["title"]
            if mb_track.get("artist"):
                values["artist"] = mb_track["artist"]
            if mb_track.get("pos") and not tags.get("tracknumber"):
                values["tracknumber"] = str(mb_track["pos"])
            if mb_track.get("disc") and not tags.get("discnumber"):
                values["discnumber"] = str(mb_track["disc"])

        if entry.get("title"):
            values["album"] = entry["title"]
        if entry.get("albumartist"):
            values["albumartist"] = entry["albumartist"]
        if entry.get("date"):
            values["date"] = entry["date"]

        # only keep fields that actually differ from what is on disk
        fresh = {k: v for k, v in values.items() if tags.get(k, "").strip() != v.strip()}
        if fresh:
            tags_written, _ = update_tags(path, fresh)
            if tags_written:
                changed_files += 1
                changed_fields += len(fresh)

    return changed_files, changed_fields


def _apply_recording(path: Path, entry: dict) -> tuple[bool, int]:
    """Loose track: update title/artist/date but never invent an album name."""
    tags = read_tags(path)
    values: dict[str, str] = {}
    if entry.get("title"):
        existing = tags.get("title") or ""
        if not existing or similarity(existing, entry["title"]) >= TITLE_THRESHOLD:
            values["title"] = entry["title"]
    if entry.get("albumartist"):
        values["artist"] = entry["albumartist"]
    if entry.get("date"):
        values["date"] = entry["date"]

    fresh = {k: v for k, v in values.items() if tags.get(k, "").strip() != v.strip()}
    if not fresh:
        return False, 0
    tags_written, _ = update_tags(path, fresh)
    return tags_written, len(fresh)


# --------------------------------------------------------------------------- #
# main task
# --------------------------------------------------------------------------- #
def scrape_metadata(
    limit: int | None = None,
    force: bool = False,
    force_covers: bool = False,
) -> dict:
    cache = load_cache()

    mp3_flat = build_file_index(MP3_ROOT, "*.mp3")
    aiff_flat = build_file_index(AIFF_ROOT, "*.aiff")
    mp3_by_album = build_album_index(mp3_flat)
    aiff_by_album = build_album_index(aiff_flat)

    groups: dict[Path, list[Path]] = {}
    for path in sorted(FLAC_ROOT.rglob("*.flac")):
        groups.setdefault(album_folder(path.parent), []).append(path)
    album_dirs = sorted(groups)

    if limit:
        album_dirs = album_dirs[:limit]

    stats = {
        "albums": len(album_dirs),
        "matched": 0,
        "no_match": 0,
        "cache_hits": 0,
        "files_tagged": 0,
        "fields_written": 0,
        "covers_local": 0,
        "covers_downloaded": 0,
        "covers_missing": 0,
    }

    save_every = 10
    progress = tqdm(album_dirs, desc="Scraping metadata", unit="album")

    for i, album_dir in enumerate(progress, start=1):
        files = groups[album_dir]
        artist, album, year, has_album = _group_identity(album_dir, files)

        entry = None
        if has_album:
            key = f"release|{normalize_name(artist)}|{normalize_name(album)}|{year}"
            entry = cache["releases"].get(key)
            if entry is None or force:
                entry = search_release(album, artist, year) or {"status": "no_match"}
                cache["releases"][key] = entry
            else:
                stats["cache_hits"] += 1
            if entry.get("status") == "matched":
                stats["matched"] += 1
                changed, fields = _apply_release(album_dir, files, entry)
                stats["files_tagged"] += changed
                stats["fields_written"] += fields
            else:
                stats["no_match"] += 1
        else:
            # Loose tracks: one recording lookup per file
            for path in files:
                tags = read_tags(path)
                title = tags.get("title") or path.stem
                track_artist = tags.get("artist") or ""
                track_year = (tags.get("date") or "")[:4] or year
                key = f"recording|{normalize_name(track_artist)}|{normalize_name(title)}"
                rec = cache["recordings"].get(key)
                if rec is None or force:
                    rec = search_recording(title, track_artist, track_year) or {"status": "no_match"}
                    cache["recordings"][key] = rec
                else:
                    stats["cache_hits"] += 1
                if rec.get("status") == "matched":
                    stats["matched"] += 1
                    changed, fields = _apply_recording(path, rec)
                    stats["files_tagged"] += int(changed)
                    stats["fields_written"] += fields
                    entry = rec  # may provide a cover via release_mbid
                else:
                    stats["no_match"] += 1

        # ---- cover art -----------------------------------------------------
        cover = find_cover(album_dir) or find_cover(files[0].parent)
        if cover is None or force_covers:
            fetched = None
            if entry is not None and entry.get("status") == "matched":
                if entry.get("cover") == "missing":
                    fetched = None  # already know CCA has nothing
                else:
                    release_id = entry.get("release_mbid", entry.get("mbid"))
                    fetched = fetch_cover(release_id, entry.get("rg_mbid"))
                    entry["cover"] = "downloaded" if fetched else "missing"

            if fetched is not None:
                _save_cover(album_dir, fetched[0], fetched[1])
                stats["covers_downloaded"] += 1
            elif cover is None:
                # harvest a cover that already exists in the mp3/aiff trees
                if _harvest_local_cover(
                    album_dir, files[0], mp3_flat, mp3_by_album, aiff_flat, aiff_by_album
                ):
                    stats["covers_local"] += 1
                else:
                    stats["covers_missing"] += 1

        if i % save_every == 0:
            save_cache(cache)

    save_cache(cache)

    print(
        "Metadata scrape complete: "
        f"{stats['matched']} matched / {stats['no_match']} unmatched groups, "
        f"{stats['files_tagged']} files tagged ({stats['fields_written']} fields), "
        f"covers: {stats['covers_downloaded']} downloaded, "
        f"{stats['covers_local']} harvested from other trees, "
        f"{stats['covers_missing']} still missing "
        f"({stats['cache_hits']} cache hits)."
    )
    return stats


if __name__ == "__main__":
    scrape_metadata()
