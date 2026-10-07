"""Enforce one canonical naming scheme across the FLAC, AIFF and MP3 trees.

Canonical scheme:

    artist/yyyy album/d-nn - track title.<ext>      (every track is prefixed
                                                     with its disc number;
                                                     single-disc / unknown
                                                     discs read as disc 1)
    bucketname/Artist - Title.<ext>                 (loose tracks with no album,
                                                     e.g. misc/, No Album/)

By default folder names *and* file names are lowercased (the tags keep their
original case, and CDJ/player displays come from the tags). Both behaviours
are opt-in switchable off via ``flatten_discs=False`` / ``lowercase=False``.

Rules:

* The FLAC tree is the source of truth: names are derived from FLAC tags
  (``tracknumber`` + ``title``), falling back to the existing file name.
* Track numbers are zero padded to the width of the widest number in the album
  (minimum 2 digits).
* Every track gets a ``<disc>-`` prefix in its name: the disc comes from the
  ``discnumber`` tag, then the disc folder name (``CD2/``, ``Disc 3:``, ...),
  and defaults to 1 when there is no disc at all - so a plain single-disc
  album is ``1-01 - Intro``, a two disc album ``1-06 - ...`` / ``2-03 - ...``.
  Multi-disc albums are folded into one folder (all ``CD*``/``Disc*``
  sub-folders are dropped). Track / disc *tags* are left untouched - matching
  against the MusicBrainz release (by disc + position) keeps working.
* Tracks living directly in a no-album bucket (``Misc/``, ``No Album/``,
  ``Singles/``, ... - any folder matched by ``NO_ALBUM_NAMES``) are named
  ``Artist - Title`` instead of ``d-nn - Title``: with no album to scope the
  title, the artist has to stay in the file name to remain identifiable.
* AIFF/MP3 counterparts are moved to the *same* relative path (extension
  swapped), including into differently named album folders - so folder names
  become identical across trees as well.
* File names are sanitized for FAT32/exFAT (CDJ USB sticks): ``<>:"|?*``,
  path separators and control characters are removed. Folder names get the
  same treatment, mirrored across all three trees.
* Pairing between trees uses normalized keys with a fuzzy fallback, so
  slight naming differences are *merged* rather than duplicated.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from tqdm import tqdm

from .matching import (
    build_album_index,
    build_file_index,
    fuzzy_match,
    normalize_rel_path,
    same_audio,
    similarity,
)
from .paths import (
    AIFF_ROOT,
    FAT_INVALID,
    FLAC_ROOT,
    MP3_ROOT,
    NO_ALBUM_NAMES,
    RENAME_HISTORY_PATH,
    RENAME_REPORT_PATH,
    prune_empty_dirs,
)
from .tags import read_tags

DISC_FOLDER_RE = re.compile(r"^(?:cd|disc|dvd|part)\s*[-.]?\s*\d+$", re.IGNORECASE)


def _stem_track_number(stem: str) -> int | None:
    """Track number embedded in a file name.

    Understands both schemes: ``02 - title`` (track 2) and the disc-prefixed
    ``1-02 - title`` (track 2, disc 1) that this module itself produces.
    """
    match = re.match(r"^(\d+)-(\d+)", stem)
    if match:
        return int(match.group(2))
    match = re.match(r"^(\d+)", stem)
    if match:
        return int(match.group(1))
    return None


# --------------------------------------------------------------------------- #
# canonical names
# --------------------------------------------------------------------------- #
def sanitize_name(name: str) -> str:
    """Make a single path component safe for FAT32 and tidy it up."""
    cleaned = "".join("_" if ch in FAT_INVALID or ch in "/\\" else ch for ch in name)
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned


def _track_number(path: Path, tags: dict) -> int | None:
    for value in (tags.get("tracknumber"), path.stem):
        if not value:
            continue
        number = _stem_track_number(str(value))
        if number is not None:
            return number
    return None


def _file_disc(path: Path, tags: dict) -> int | None:
    """Disc number of a file, from the ``discnumber`` tag or its folder name."""
    match = re.search(r"\d+", tags.get("discnumber") or "")
    if match:
        return int(match.group())
    if DISC_FOLDER_RE.match(path.parent.name):
        match = re.search(r"\d+", path.parent.name)
        if match:
            return int(match.group())
    return None


def _fmt_component(name: str, lowercase: bool) -> str:
    """Sanitize a folder/file component, optionally lowercasing it."""
    cleaned = sanitize_name(name)
    return cleaned.lower() if lowercase else cleaned


def canonical_stem(path: Path, tags: dict, width: int, *,
                   disc: int | None = None, prefix_disc: bool = False,
                   lowercase: bool = False) -> str | None:
    """Build ``NN - Title`` (or ``D-NN - Title``) for a file, or None."""
    number = _track_number(path, tags)
    title = (tags.get("title") or "").strip()

    if not title:
        # strip an existing "NN - " or "D-NN - " prefix, keep the rest as-is
        title = re.sub(r"^\d+(?:-\d+)?\s*[-._)]\s*", "", path.stem).strip()
    if not title:
        return None

    title = sanitize_name(title)
    if not title:
        return None
    if lowercase:
        title = title.lower()

    prefix = f"{disc}-" if (prefix_disc and disc) else ""
    if number is None:
        return prefix + title
    return f"{prefix}{number:0{width}d} - {title}"


def loose_stem(path: Path, tags: dict, *, lowercase: bool = False) -> str | None:
    """``Artist - Title`` for tracks living in no-album buckets (loose tracks).

    Loose tracks have no album to scope the title, so the artist has to live
    in the file name to stay identifiable - especially in mixed-artist buckets
    like ``various artists/no album``. Falls back to title only (or the
    existing file name) when a tag is missing.
    """
    artist = (tags.get("artist") or "").strip()
    title = (tags.get("title") or "").strip()
    if not title:
        # keep the existing name, minus an old "NN - " / "D-NN - " prefix
        title = re.sub(r"^\d+(?:-\d+)?\s*[-._)]\s*", "", path.stem).strip()
    if not title:
        return None
    title = sanitize_name(title)
    if not title:
        return None
    if artist:
        artist = sanitize_name(artist)
        # avoid "acopia - acopia - falter" when the fallback title already
        # carried the artist prefix
        if title.lower().startswith((artist + " - ").lower()):
            title = title[len(artist) + 3:].strip()
    if not title:
        return None
    if lowercase:
        title = title.lower()
        artist = artist.lower()
    if artist:
        return f"{artist} - {title}"
    return title


def _number_width(files: list[Path]) -> int:
    largest = 0
    for path in files:
        number = _stem_track_number(path.stem)
        if number is not None:
            largest = max(largest, len(str(number)))
        tags = read_tags(path)
        if tags.get("tracknumber"):
            m = re.search(r"\d+", tags["tracknumber"])
            if m:
                largest = max(largest, len(m.group()))
    return max(2, largest)


# --------------------------------------------------------------------------- #
# counterpart lookup (aiff/mp3 twin of a flac file)
# --------------------------------------------------------------------------- #
def _counterpart(flac_file: Path, rel_key: str, flat: dict, grouped: dict) -> Path | None:
    exact = flat.get(rel_key)
    if exact is not None:
        return exact
    twin = fuzzy_match(rel_key, grouped)
    # only accept a fuzzy match if it really contains the same recording,
    # otherwise the move would merge two different tracks under one name
    if twin is not None and same_audio(flac_file, twin):
        return twin
    return None


def _flush_rename_history(lines: list[str]) -> None:
    """Append renames to the cumulative ``rename_history.txt``.

    Keeps every generation of renames so a path from *any* earlier export can
    be translated to the current tree (used by :mod:`relocate_rekordbox`).
    The previous run's ``rename_report.txt`` - possibly written by an older
    version that kept no history - is migrated in first, then this run's own
    renames, both deduplicated so re-running is idempotent.
    """
    seen: set[str] = set()
    if RENAME_HISTORY_PATH.exists():
        seen.update(RENAME_HISTORY_PATH.read_text(encoding="utf-8").splitlines())
    additions: list[str] = []
    if RENAME_REPORT_PATH.exists():
        for line in RENAME_REPORT_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and line not in seen:
                additions.append(line)
                seen.add(line)
    for line in lines:
        if line not in seen:
            additions.append(line)
            seen.add(line)
    if additions:
        RENAME_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RENAME_HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n".join(additions) + "\n")


# --------------------------------------------------------------------------- #
# task
# --------------------------------------------------------------------------- #
def normalize_names(limit: int | None = None, dry_run: bool = False,
                    flatten_discs: bool = True, lowercase: bool = True) -> dict:
    flac_flat = build_file_index(FLAC_ROOT, "*.flac")
    aiff_flat = build_file_index(AIFF_ROOT, "*.aiff")
    mp3_flat = build_file_index(MP3_ROOT, "*.mp3")
    aiff_by_album = build_album_index(aiff_flat)
    mp3_by_album = build_album_index(mp3_flat)

    # group flac files per album folder (walking out of CD1/CD2 sub-folders)
    groups: dict[Path, list[Path]] = {}
    for key in sorted(flac_flat):
        path = flac_flat[key]
        folder = path.parent
        while DISC_FOLDER_RE.match(folder.name) and folder != FLAC_ROOT:
            folder = folder.parent
        groups.setdefault(folder, []).append(path)

    renames: list[tuple[Path, Path, Path]] = []  # (root, source, dest) in the flac tree
    moved: list[tuple[Path, Path, Path]] = []  # counterpart moves in aiff/mp3 trees
    skipped: list[tuple[Path, str]] = []

    album_dirs = sorted(groups)
    if limit:
        album_dirs = album_dirs[:limit]

    for album_dir in tqdm(album_dirs, desc="Planning renames", unit="album"):
        files = sorted(groups[album_dir])
        is_bucket = (
            album_dir == FLAC_ROOT
            or album_dir.name.lower().strip() in NO_ALBUM_NAMES
        )
        width = _number_width(files) if not is_bucket else 2

        # ---- folder name (FAT32 sanitizing + optional lowercase) ----------
        rel_parts = album_dir.relative_to(FLAC_ROOT).parts
        safe_parts = tuple(_fmt_component(part, lowercase) for part in rel_parts)
        safe_album_dir = FLAC_ROOT.joinpath(*safe_parts) if safe_parts else FLAC_ROOT

        # ---- per-file disc number (''-> 1 below when flattening) ----------
        file_tags = {path: read_tags(path) for path in files}
        disc_of = (
            {path: _file_disc(path, file_tags[path]) for path in files}
            if not is_bucket
            else {}
        )

        for path in files:
            rel = path.relative_to(FLAC_ROOT)
            tags = file_tags[path]

            if is_bucket:
                stem = loose_stem(path, tags, lowercase=lowercase)
                if stem is None:
                    skipped.append((path, "could not determine title"))
                    continue
                new_dir = safe_album_dir
            else:
                disc = disc_of[path]
                if flatten_discs and disc is None:
                    disc = 1  # single-disc albums / unknown discs read as disc 1
                stem = canonical_stem(
                    path, tags, width,
                    disc=disc,
                    prefix_disc=flatten_discs,
                    lowercase=lowercase,
                )
                if stem is None:
                    skipped.append((path, "could not determine title"))
                    continue

                new_dir = safe_album_dir
                if not flatten_discs and path.parent != album_dir:
                    new_dir = new_dir / _fmt_component(path.parent.name, lowercase)

            dst = new_dir / (stem + path.suffix)
            if dst != path:
                renames.append((FLAC_ROOT, path, dst))

            # counterparts follow the flac rel path exactly
            rel_key = normalize_rel_path(rel)
            for root, ext, grouped, flat in (
                (AIFF_ROOT, ".aiff", aiff_by_album, aiff_flat),
                (MP3_ROOT, ".mp3", mp3_by_album, mp3_flat),
            ):
                twin = _counterpart(path, rel_key, flat, grouped)
                if twin is None:
                    continue
                twin_rel = new_dir.relative_to(FLAC_ROOT)
                twin_dst = root / twin_rel / (stem + ext)
                if twin_dst != twin:
                    moved.append((root, twin, twin_dst))

    # ---- de-duplicate and drop impossible moves ---------------------------
    renames = sorted(set(renames))
    moved = sorted(set(moved))

    def _is_real_collision(src: Path, dst: Path) -> bool:
        """True only if a *different* file already sits at *dst*.

        Case-only renames must not count as collisions on macOS's
        case-insensitive APFS, otherwise casing fixes would always be skipped.
        """
        if not dst.exists():
            return False
        try:
            return not os.path.samefile(src, dst)
        except OSError:
            return True

    collisions = []
    final_renames, final_moves = [], []
    for root, src, dst in renames:
        if _is_real_collision(src, dst):
            collisions.append((src, dst))
        else:
            final_renames.append((root, src, dst))
    for root, src, dst in moved:
        if _is_real_collision(src, dst):
            collisions.append((src, dst))
        else:
            final_moves.append((root, src, dst))

    def _ensure_cased_parents(root: Path, dst: Path) -> None:
        """Create the destination folder chain, fixing case-only name drift.

        On case-insensitive filesystems a folder can exist with the wrong case
        (``nas`` instead of ``Nas``); a bare mkdir then no-ops and the OS keeps
        the old casing. If a same-inode sibling with different casing exists it
        is renamed through a temp name so the new casing sticks. Distinct
        folders that merely look alike (case-sensitive filesystems) are never
        touched - ``samefile`` says they are different dirs.
        """
        cur = root
        if not dst.parent.is_relative_to(root):
            return
        for part in dst.relative_to(root).parent.parts:
            target = cur / part
            if cur.exists():
                for child in cur.iterdir():
                    if (
                        child.name != part
                        and child.name.lower() == part.lower()
                        and os.path.samefile(child, target)
                    ):
                        tmp = cur / (child.name + ".__casefix__")
                        child.rename(tmp)
                        tmp.rename(target)
                        break
            target.mkdir(exist_ok=True)
            cur = target

    def _rename(root: Path, src: Path, dst: Path) -> None:
        _ensure_cased_parents(root, dst)
        if src == dst:
            return
        try:
            src.rename(dst)
        except OSError:
            # case-only renames on some filesystems need a two-step dance
            tmp = src.parent / (src.name + ".__tmp_rename__")
            src.rename(tmp)
            tmp.rename(dst)

    lines = []
    for root, src, dst in final_renames + final_moves:
        lines.append(f"{src} -> {dst}")

    if dry_run:
        print(f"Dry run: would perform {len(final_renames)} flac renames and "
              f"{len(final_moves)} mp3/aiff moves.")
    else:
        for root, src, dst in tqdm(final_renames + final_moves, desc="Renaming", unit="file"):
            try:
                _rename(root, src, dst)
            except OSError as exc:
                skipped.append((src, str(exc)))

        removed_aiff = prune_empty_dirs(AIFF_ROOT)
        removed_mp3 = prune_empty_dirs(MP3_ROOT)
        removed_flac = prune_empty_dirs(FLAC_ROOT)

        _flush_rename_history(lines)

        if lines:
            RENAME_REPORT_PATH.write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

        print(
            f"Renamed {len(final_renames)} FLAC files, moved "
            f"{len(final_moves)} MP3/AIFF counterparts, removed "
            f"{removed_flac + removed_aiff + removed_mp3} empty folders."
        )
        if lines:
            print(f"Full rename log: {RENAME_REPORT_PATH}")
            for line in lines[:15]:
                print(f"    {line}")
            if len(lines) > 15:
                print(f"    ... and {len(lines) - 15} more")

    for path, reason in skipped[:20]:
        print(f"    SKIPPED {path}: {reason}")
    for src, dst in collisions[:20]:
        print(f"    COLLISION {src} -> {dst} (destination already exists)")

    return {
        "renamed": len(final_renames),
        "moved": len(final_moves),
        "skipped": len(skipped),
        "collisions": len(collisions),
        "empty_dirs_removed": 0 if dry_run else removed_flac + removed_aiff + removed_mp3,
    }


if __name__ == "__main__":
    normalize_names()
