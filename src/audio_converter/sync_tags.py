"""Propagate FLAC tags and album covers into the MP3 and AIFF trees.

The FLAC tree is the single source of truth. After this task, every MP3 and
AIFF file carries the same tags and the same embedded cover art, and the album
folder in both trees carries the same ``cover.jpg``.

It also aligns modification times (mp3/aiff = max(flac mtime, cover mtime)) so
``sync_library`` does not consider those files stale and re-encode audio that
has not actually changed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tqdm import tqdm

from .paths import (
    AIFF_ROOT,
    FLAC_ROOT,
    MP3_ROOT,
    find_cover,
    read_cover as read_folder_cover,
)
from .tags import read_tags, update_tags

TARGETS = ((MP3_ROOT, ".mp3"), (AIFF_ROOT, ".aiff"))


def sync_tags(limit: int | None = None, replace_cover: bool = False) -> dict:
    flac_files = sorted(FLAC_ROOT.rglob("*.flac"))
    if limit:
        flac_files = flac_files[:limit]

    stats = {"tags_written": 0, "covers_written": 0, "covers_copied": 0,
             "files_updated": 0, "files_missing": 0, "files_clean": 0}

    for flac in tqdm(flac_files, desc="Syncing tags", unit="file"):
        rel = flac.relative_to(FLAC_ROOT)
        values = read_tags(flac)

        # Timestamp that both "is it newer than the flac?" and "is it newer than
        # the cover?" checks must fail after we are done.
        align = flac.stat().st_mtime
        cover = read_folder_cover(flac.parent)
        cover_file = find_cover(flac.parent)
        if cover_file is not None:
            align = max(align, cover_file.stat().st_mtime)

        updated = False
        for root, ext in TARGETS:
            target = root / rel.with_suffix(ext)
            if not target.exists():
                stats["files_missing"] += 1
                continue

            tags_written, cover_written = update_tags(
                target, values, cover, replace_cover=replace_cover
            )
            stats["tags_written"] += int(tags_written)
            stats["covers_written"] += int(cover_written)
            updated = updated or tags_written or cover_written

            # Album folder image in the target tree
            if cover_file is not None and find_cover(target.parent) is None:
                shutil.copy2(cover_file, target.parent / cover_file.name)
                stats["covers_copied"] += 1

            os.utime(target, (align, align))

        if updated:
            stats["files_updated"] += 1
        else:
            stats["files_clean"] += 1

    print(
        "Tag sync complete: "
        f"{stats['files_updated']} files updated, "
        f"{stats['files_clean']} already consistent, "
        f"{stats['tags_written']} tag writes, "
        f"{stats['covers_written']} covers embedded, "
        f"{stats['covers_copied']} folder covers copied, "
        f"{stats['files_missing']} counterparts missing (sync_library will create them)."
    )
    return stats


if __name__ == "__main__":
    sync_tags()
