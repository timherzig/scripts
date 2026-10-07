"""Complete the lossless trees with MP3s that have no FLAC/AIFF counterpart.

Roughly 1000 MP3s exist without a lossless twin. This task transcodes them to
FLAC and AIFF so all three trees cover the same collection, and records every
such file in ``lossy_sourced_manifest.json``. Those files are bit-perfect
copies of a *lossy* source, so the manifest lets you replace them with real
lossless rips later (``check_lossy`` reports on them).

Matching is done on normalized keys with a fuzzy fallback, so an MP3 whose
lossless twin simply has a slightly different name is *not* converted twice -
it is skipped here and straightened out by ``normalize_names``.
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from .flac_to_mp3 import convert_flac_to_aiff, copy_cover
from .matching import build_album_index, build_file_index, fuzzy_match, normalize_rel_path, same_audio
from .paths import AIFF_ROOT, FLAC_ROOT, MANIFEST_PATH, MP3_ROOT, find_cover, read_cover
from .tags import update_tags


# --------------------------------------------------------------------------- #
# single-file conversion (runs in a worker process)
# --------------------------------------------------------------------------- #
def _convert_one(mp3_rel: str) -> dict:
    """mp3 -> flac -> aiff, preserving tags, cover art and mtime."""
    rel = Path(mp3_rel)
    mp3 = MP3_ROOT / rel
    flac = FLAC_ROOT / rel.with_suffix(".flac")
    aiff = AIFF_ROOT / rel.with_suffix(".aiff")

    flac.parent.mkdir(parents=True, exist_ok=True)

    # 1. mp3 -> flac, carrying the ID3 metadata over as Vorbis comments
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(mp3),
            "-map_metadata", "0",
            "-map", "0:a",
            "-c:a", "flac",
            str(flac),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 2. keep the source mtime so sync_library does not see it as stale
    st = mp3.stat()
    os.utime(flac, (st.st_atime, st.st_mtime))

    # 3. album cover: harvest from the mp3 folder, embed into the flac
    cover = find_cover(mp3.parent)
    if cover is not None:
        copy_cover(cover, flac.parent)
        update_tags(flac, {}, cover=read_cover(mp3.parent))

    # 4. flac -> aiff (same converter used by sync_library)
    convert_flac_to_aiff(flac, aiff, cover)

    # Record the *produced* flac's stat + sample count. check_lossy uses those
    # to detect a real lossless rip being dropped over the file: a tag edit
    # changes size/mtime but not the number of audio samples.
    st = flac.stat()
    try:
        from mutagen.flac import FLAC as _FLAC
        samples = _FLAC(flac).info.total_samples
    except Exception:
        samples = None

    return {
        "rel": rel.with_suffix(".flac").as_posix(),
        "source_mp3": rel.as_posix(),
        "source_size": mp3.stat().st_size,
        "source_mtime": mp3.stat().st_mtime,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "samples": samples,
    }


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"entries": {}}


def save_manifest(manifest: dict) -> None:
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
def acquire_missing(limit: int | None = None) -> dict:
    print("Indexing FLAC tree...")
    flac_index = build_file_index(FLAC_ROOT, "*.flac")
    flac_by_album = build_album_index(flac_index)

    print("Scanning MP3 tree...")
    mp3_files = sorted(MP3_ROOT.rglob("*.mp3"))

    pending: list[Path] = []
    name_mismatch: list[tuple[Path, Path]] = []

    for mp3 in mp3_files:
        rel = mp3.relative_to(MP3_ROOT)
        key = normalize_rel_path(rel)
        if key in flac_index:
            continue
        twin = fuzzy_match(key, flac_by_album)
        if twin is not None and same_audio(mp3, twin):
            # fuzzy twin really is the same recording under a different name -
            # skip the conversion, normalize_names will merge them
            name_mismatch.append((rel, twin))
            continue
        pending.append(rel)
        if limit and len(pending) >= limit:
            break

    if name_mismatch:
        print(
            f"{len(name_mismatch)} MP3s already have a lossless twin under a "
            "slightly different name - they will be renamed by normalize_names."
        )
        for rel, twin in name_mismatch[:5]:
            print(f"    {rel}  ->  {twin.relative_to(FLAC_ROOT)}")

    if not pending:
        print("Every MP3 already has a FLAC counterpart.")
        return {"converted": 0, "name_mismatch": len(name_mismatch)}

    print(f"Converting {len(pending)} MP3s to FLAC + AIFF...")

    manifest = load_manifest()
    converted, failed = [], []
    max_workers = os.cpu_count() or 4

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_convert_one, rel.as_posix()): rel for rel in pending}
        with tqdm(total=len(futures), desc="Acquiring lossless", unit="file") as pbar:
            for i, future in enumerate(as_completed(futures), start=1):
                rel = futures[future]
                try:
                    entry = future.result()
                    manifest["entries"][entry["rel"]] = entry
                    converted.append(entry["rel"])
                except Exception as exc:  # keep going, report at the end
                    failed.append((rel.as_posix(), str(exc)))
                    tqdm.write(f"Error converting {rel}: {exc}")
                if i % 25 == 0:
                    save_manifest(manifest)
                pbar.update(1)

    save_manifest(manifest)

    print(
        f"Converted {len(converted)} tracks to FLAC + AIFF"
        + (f", {len(failed)} failed" if failed else "")
        + f". Manifest: {MANIFEST_PATH}"
    )
    for rel, err in failed[:10]:
        print(f"    FAILED {rel}: {err}")

    return {
        "converted": len(converted),
        "failed": len(failed),
        "name_mismatch": len(name_mismatch),
    }


def check_lossy_manifest(prune: bool = False) -> dict:
    """Report which FLAC files were created from a lossy MP3 source.

    An entry counts as "replaced" once its size or mtime changed, i.e. you
    dropped a real lossless rip over the generated file.
    """
    manifest = load_manifest()
    entries: dict = manifest.get("entries", {})

    still_lossless: dict = {}
    replaced: list[str] = []
    removed: list[str] = []

    for rel, entry in sorted(entries.items()):
        path = FLAC_ROOT / rel
        if not path.exists():
            removed.append(rel)
            continue

        def _unchanged() -> bool:
            if entry.get("samples") is not None:
                try:
                    from mutagen.flac import FLAC as _FLAC
                    return _FLAC(path).info.total_samples == entry["samples"]
                except Exception:
                    return False
            # fallback for manifests written before samples were recorded
            st = path.stat()
            return st.st_size == entry.get("size") and abs(st.st_mtime - entry.get("mtime", -1)) < 1

        if _unchanged():
            still_lossless[rel] = entry
        else:
            replaced.append(rel)

    print("=" * 60)
    print("   LOSSY-SOURCED FLAC AUDIT")
    print("=" * 60)
    print(f"Flagged files:        {len(entries)}")
    print(f"Still lossy sources:  {len(still_lossless)}")
    print(f"Likely replaced:      {len(replaced)}")
    print(f"Removed:              {len(removed)}")

    by_album: dict[str, int] = {}
    for rel in still_lossless:
        album = str(Path(rel).parent)
        by_album[album] = by_album.get(album, 0) + 1
    if by_album:
        print("\nAlbums still awaiting a real lossless rip:")
        for album, count in sorted(by_album.items()):
            print(f"    [{count:>3} tracks] {album}")

    if prune and (replaced or removed):
        manifest["entries"] = still_lossless
        save_manifest(manifest)
        print(f"\nPruned {len(replaced) + len(removed)} resolved entries from the manifest.")

    return {
        "flagged": len(entries),
        "still_lossy": len(still_lossless),
        "replaced": len(replaced),
        "removed": len(removed),
    }


if __name__ == "__main__":
    acquire_missing()
