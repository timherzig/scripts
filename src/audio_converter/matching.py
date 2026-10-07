"""Cross-tree matching helpers.

The same track exists three times (``.flac`` / ``.mp3`` / ``.aiff``). To link
them we compare *normalized relative keys*: path without extension, lowercased,
with every non-word character removed. That makes ``01 - Life's a Bitch.mp3``
and ``01 - Life’s A Bitch.flac`` resolve to the same key.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path


def normalize_rel_path(path: Path | str) -> str:
    """Extension-insensitive, punctuation-insensitive key for a relative path."""
    clean = str(Path(path).with_suffix("")).lower()
    return re.sub(r"[^\w/]", "", clean)


def audio_length(path: Path) -> float | None:
    """Duration in seconds via mutagen (no subprocess), None on parse errors."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".flac":
            from mutagen.flac import FLAC
            return FLAC(path).info.length
        if suffix in (".aiff", ".aif"):
            from mutagen.aiff import AIFF
            return AIFF(path).info.length
        from mutagen.mp3 import MP3
        return MP3(path).info.length
    except Exception:
        return None


def same_audio(a: Path, b: Path, tolerance: float = 1.5, ratio: float = 0.01) -> bool:
    """Do two files almost certainly contain the same recording?

    Used to decide whether a fuzzy name match really is the same track.
    If either duration is unreadable we optimistically accept (the name match
    already had to be strong).
    """
    len_a, len_b = audio_length(a), audio_length(b)
    if len_a is None or len_b is None:
        return True
    return abs(len_a - len_b) <= tolerance + ratio * max(len_a, len_b)


def normalize_name(text: str) -> str:
    """Loose comparison key for a single name (title / artist / album)."""
    return re.sub(r"[^\w]", "", (text or "").lower())


def similarity(a: str, b: str) -> float:
    """Similarity ratio of two strings, ignoring case and punctuation."""
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def build_file_index(root: Path, pattern: str) -> dict[str, Path]:
    """Map normalized relative key -> file path for every match under *root*."""
    index: dict[str, Path] = {}
    if not root.exists():
        return index
    for path in root.rglob(pattern):
        try:
            key = normalize_rel_path(path.relative_to(root))
        except ValueError:
            continue
        index[key] = path
    return index


def album_key(rel_key: str) -> str:
    """Album folder part of a normalized key ('' for loose tracks)."""
    return rel_key.rsplit("/", 1)[0] if "/" in rel_key else ""


def track_key(rel_key: str) -> str:
    """File-name part (without extension) of a normalized key."""
    return rel_key.rsplit("/", 1)[-1]


def build_album_index(index: dict[str, Path]) -> dict[str, list[tuple[str, Path]]]:
    """Group a file index by album folder: album key -> [(track key, path)]."""
    grouped: dict[str, list[tuple[str, Path]]] = {}
    for key, path in index.items():
        grouped.setdefault(album_key(key), []).append((key, path))
    return grouped


def fuzzy_match(
    rel_key: str,
    by_album: dict[str, list[tuple[str, Path]]],
    track_threshold: float = 0.75,
    album_threshold: float = 0.9,
) -> Path | None:
    """Find a near-identical track inside the same (or a similarly named) album.

    Used to bridge small naming differences between the trees, e.g. an mp3
    called ``01 - Intro`` while the flac is ``01 - Intro (Rmx)``.

    The album folder must match quite strictly (0.9) so that similarly named
    but genuinely different releases (``1994 Illmatic`` vs ``1995 Illmatic``)
    are never treated as the same album.
    """
    a_key = album_key(rel_key)
    candidates = by_album.get(a_key)

    if candidates is None:
        # Try to find a similarly named album folder
        best_album, best_ratio = "", 0.0
        for other in by_album:
            ratio = SequenceMatcher(None, a_key, other).ratio()
            if ratio > best_ratio:
                best_album, best_ratio = other, ratio
        if best_ratio < album_threshold:
            return None
        candidates = by_album[best_album]

    t_key = track_key(rel_key)
    for key, path in candidates:
        if SequenceMatcher(None, t_key, track_key(key)).ratio() >= track_threshold:
            return path
    return None
