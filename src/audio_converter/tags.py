"""Read and write tags consistently across FLAC, MP3 and AIFF.

Canonical fields (Vorbis-comment names) are mapped onto ID3 frames for the two
lossy/CDJ-friendly formats. Everything is written as ID3v2.3 because that is
what CDJs and the existing ffmpeg conversions use.

Gotchas handled here:

* ``mutagen.id3.ID3`` on an AIFF file does **not** understand the FORM chunk and
  would prepend a tag at byte 0 (corrupting the file) - AIFF always goes through
  ``mutagen.aiff.AIFF``.
* ID3v2.4 frames (e.g. ``TDRC``) must pass through ``update_to_v23()`` before a
  v2.3 save; mutagen re-reads ``TYER``/``TDAT`` as ``TDRC`` so dates round-trip.
* Only the frames we manage are replaced, so third-party frames (ReplayGain,
  Discogs IDs, BPM, comments...) are left untouched.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3,
    ID3NoHeaderError,
    APIC,
    TALB,
    TCON,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TDRC,
)

# Canonical field name -> ID3 frame class
FIELDS = (
    "title",
    "artist",
    "album",
    "albumartist",
    "tracknumber",
    "discnumber",
    "date",
    "genre",
)
ID3_FRAMES = {
    "title": TIT2,
    "artist": TPE1,
    "album": TALB,
    "albumartist": TPE2,
    "tracknumber": TRCK,
    "discnumber": TPOS,
    "date": TDRC,
    "genre": TCON,
}
FLAC_FRAMES = {field: field for field in FIELDS}


def _join(values) -> str:
    """Flatten a frame's text values into one string."""
    if values is None:
        return ""
    if isinstance(values, str):
        return values.strip()
    return "; ".join(str(v) for v in values if str(v)).strip()


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _is_aiff(path: Path) -> bool:
    return path.suffix.lower() in {".aiff", ".aif"}


def read_tags(path: Path) -> dict[str, str]:
    """Return ``{canonical_field: value}`` for any supported audio file."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            audio = FLAC(path)
            return {
                field: _join(audio.get(key))
                for field, key in FLAC_FRAMES.items()
                if _join(audio.get(key))
            }
        if _is_aiff(path):
            audio = AIFF(path)
            id3 = audio.tags
        else:
            id3 = ID3(path)
        if id3 is None:
            return {}
        out: dict[str, str] = {}
        for field, frame_cls in ID3_FRAMES.items():
            frame = id3.get(frame_cls.__name__)
            if frame is not None:
                value = _join(frame.text)
                if value:
                    out[field] = value
        return out
    except (ID3NoHeaderError, OSError, ValueError):
        return {}


def read_cover(path: Path) -> bytes | None:
    """Return the embedded front-cover bytes, if any."""
    try:
        if path.suffix.lower() == ".flac":
            pictures = FLAC(path).pictures
            for pic in pictures:
                if pic.type == 3:
                    return pic.data
            return pictures[0].data if pictures else None

        if _is_aiff(path):
            id3 = AIFF(path).tags
        else:
            id3 = ID3(path)
        if id3 is None:
            return None
        front = id3.getall("APIC")
        for pic in front:
            if pic.type == 3:
                return pic.data
        return front[0].data if front else None
    except (ID3NoHeaderError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def _open_id3(path: Path):
    """Return ``(owner, id3_tags)`` where *owner* is None for plain MP3 files."""
    if _is_aiff(path):
        audio = AIFF(path)
        if audio.tags is None:
            audio.add_tags()
        return audio, audio.tags
    try:
        return None, ID3(path)
    except ID3NoHeaderError:
        return None, ID3()


def _save_id3(path: Path, owner, id3) -> None:
    id3.update_to_v23()
    if owner is not None:
        owner.save(v2_version=3)
    else:
        id3.save(path, v2_version=3)


def _cover_frame_has(id3, data: bytes) -> bool:
    for pic in id3.getall("APIC"):
        if pic.data == data:
            return True
    return False


def update_tags(
    path: Path,
    values: dict[str, str],
    cover: tuple[bytes, str] | None = None,
    replace_cover: bool = False,
) -> tuple[bool, bool]:
    """Write canonical *values* (and optionally the cover) to *path*.

    Empty/missing values are never written, frames we do not manage are never
    touched. Returns ``(tags_written, cover_written)``.
    """
    suffix = path.suffix.lower()

    # ---- FLAC -------------------------------------------------------------
    if suffix == ".flac":
        audio = FLAC(path)
        tags_written = False
        for field, key in FLAC_FRAMES.items():
            new = (values.get(field) or "").strip()
            if not new:
                continue
            if _join(audio.get(key)) != new:
                audio[key] = [new]
                tags_written = True

        cover_written = False
        if cover is not None:
            data, mime = cover
            existing = audio.pictures
            if not existing or (replace_cover and not any(p.data == data for p in existing)):
                audio.clear_pictures()
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover (front)"
                pic.data = data
                audio.add_picture(pic)
                cover_written = True

        if tags_written or cover_written:
            audio.save()
        return tags_written, cover_written

    # ---- MP3 / AIFF -------------------------------------------------------
    owner, id3 = _open_id3(path)

    tags_written = False
    for field, frame_cls in ID3_FRAMES.items():
        new = (values.get(field) or "").strip()
        if not new:
            continue
        frame = id3.get(frame_cls.__name__)
        if frame is None or _join(frame.text) != new:
            id3.delall(frame_cls.__name__)
            id3.add(frame_cls(encoding=3, text=new))
            tags_written = True

    cover_written = False
    if cover is not None:
        data, mime = cover
        if not _cover_frame_has(id3, data):
            if id3.getall("APIC") and not replace_cover:
                pass  # keep the cover that is already embedded
            else:
                id3.delall("APIC")
                id3.add(
                    APIC(
                        encoding=3,
                        mime=mime,
                        type=3,
                        desc="Cover (front)",
                        data=data,
                    )
                )
                cover_written = True

    if tags_written or cover_written:
        _save_id3(path, owner, id3)
    return tags_written, cover_written
