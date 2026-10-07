# Audio library maintenance + Rekordbox migration

Scripts for a three-tree music library — every track exists three times, once
per tree:

| Tree  | Path                   | Purpose                                   |
|-------|------------------------|-------------------------------------------|
| FLAC  | `<root>/flac`          | Source of truth (best quality, tags)      |
| AIFF  | `<root>/aiff`          | 16-bit / 44.1 kHz, for CDJs & Rekordbox   |
| MP3   | `<root>/mp3`           | Portable fallback                         |

`<root>` defaults to `/Volumes/nas/media/music` and can be overridden with the
`MUSIC_ROOT` environment variable (the sandbox tests rely on this).

## Setup

```sh
cd ~/Documents/Projects/Misc/scripts
python3 -m venv .venv
.venv/bin/pip install -e .          # imports mutagen, tqdm
# ffmpeg/ffprobe must be on PATH
```

`uv` is also fully supported — the project exposes a proper `pyproject.toml`,
so `uv run python main.py -t pipeline` (or `uv sync` first) works the same as
`./.venv/bin/python main.py`:

## The pipeline

Run everything, in dependency order:

```sh
.venv/bin/python main.py -t pipeline
```

Each stage is idempotent and can be run on its own with `-t <task>`:

1. **`acquire_missing`** – every MP3 that has no FLAC/AIFF twin is transcoded to
   FLAC + AIFF (tags and mtime preserved). Files whose twin merely has a
   different name are *skipped* here and merged by `normalize_names`; a fuzzy
   name match is only accepted as "same recording" when the durations agree
   (`matching.same_audio`, tolerance 1.5 s + 1 %). Every generated file is
   recorded in `lossy_sourced_manifest.json` so it can later be replaced by a
   real lossless rip (see `check_lossy`).

2. **`scrape_metadata`** – MusicBrainz release search + Cover Art Archive, only
   touching the FLAC tree. Album/albumartist/date always come from the matched
   release; track titles are only replaced when missing or near-identical;
   genres are never overwritten. Multi-disc releases (album tag like
   `3 Feet High And Rising (CD 1)`) are looked up under the plain album title.
   Results and covers are cached in `.musicbrainz_cache.json`; MB is rate
   limited to 1 request/s as required by its terms of service. Unmatched
   albums fall back to harvesting a cover that already exists in the MP3/AIFF
   trees.

3. **`sync_tags`** – pushes FLAC tags + embedded cover art to every MP3/AIFF
   counterpart and aligns mtimes, so `sync_library` never re-encodes audio that
   only had its metadata updated.

4. **`normalize_names`** – one canonical naming scheme across all three trees,
   derived from the FLAC tree (which is the source of truth):

   ```
   artist/yyyy album/d-nn - track title.<ext>      (every track is prefixed
                                                   with its disc number;
                                                   single-disc / unknown
                                                   discs read as disc 1)
   bucketname/Artist - Title.<ext>                 (loose tracks with no album,
                                                   e.g. misc/, No Album/)
   ```

   Names (and folder names) are lowercased — the tags keep their original case,
   and players/CDJs display from the tags. Loose tracks in no-album buckets
   (`misc/`, `No Album/`, `Singles/`, ...) keep `Artist - Title` instead of a
   disc-prefixed number, since there is no album to scope the title. Track
   numbers are zero-padded to the
   width of the widest number (min 2). Every file is prefixed with its disc
   number (from the `discnumber` tag, then the disc folder name, defaulting to
   `1` when there is no disc at all): a plain single-disc album is
   `1-01 - Intro`, a two-disc album `1-06 - Intro` / `2-03 - Rmx`. Multi-disc
   albums are folded into a single folder — `CD1/`/`Disc 2:` sub-folders are
   dropped — while `tracknumber`/`discnumber` tags remain per-disc (MusicBrainz
   + Rekordbox disc-position matching still works). MP3/AIFF counterparts are
   moved to the *same* relative path as their FLAC twin (fuzzy pairs accepted
   only when `same_audio` agrees). Folder names are additionally sanitized for
   FAT32/exFAT (`<>:"|?*`), and case drift is fixed even on
   case-insensitive filesystems (a same-inode sibling with the wrong case is
   renamed via a temp name). Empty folders are pruned. Every change is logged
   to `rename_report.txt` and accumulated in `rename_history.txt` (append-only
   across runs), so a filename from *any* earlier state can be traced to its
   current one.

5. **`sync_library`** – converts any FLAC that has no (or a stale) MP3/AIFF
   twin. Re-runs are no-ops once mtimes are aligned.

6. **`find_missing_lossless`** – scans the MP3 tree and writes
   `missing_lossless_report.txt` with every track that still lacks a FLAC/AIFF
   twin (both album folders and loose tracks).

7. **`relocate_rekordbox`** – repoints the Rekordbox XML collection from MP3 to
   AIFF. Matching strategies in order: rename-history translation (each track's path
   is traced through `rename_history.txt` — covering *every* rename from any
   earlier run, incl. disc-folder flattening and compilation
   `NN - Artist - Title` rewrites — straight to its current AIFF twin) →
   exact path mirror → normalized match via the FLAC tree (with fuzzy
   fallback, both guarded by `same_audio` so a different recording is never
   linked) → artist+title lookup against the AIFF tags. Track attributes (Name, Artist, Album, Genre, Year) are refreshed from
   the target AIFF's tags; `TrackID` (and therefore playlists) is untouched.
   Tracks that cannot be matched are written to `rekordbox_unmatched.txt`
   instead of being silently dropped.

   ```sh
   # export the collection first: rekordbox -> File -> Export -> Collection
   .venv/bin/python main.py -t relocate_rekordbox \
     --xml-in ~/Desktop/rekordbox.xml \
     --xml-out ~/Desktop/rekordbox_aiff.xml
   ```

### Useful flags

| Flag            | Applies to              | Effect                                        |
|-----------------|-------------------------|-----------------------------------------------|
| `--limit N`     | scrape, sync_tags, normalize, acquire | only the first N albums/files |
| `--force`       | scrape                  | re-query MusicBrainz even if cached           |
| `--force-covers`| scrape / sync_tags      | re-download / re-embed covers                 |
| `--dry-run`     | normalize_names         | print planned renames without touching files  |
| `--no-flatten-discs` | normalize_names    | keep `CD1/`/`CD2/` sub-folders (default: fold multi-disc albums into one folder) |
| `--no-lowercase` | normalize_names         | keep Mixed Case file and folder names (default: lowercase) |
| `--prune`       | check_lossy             | drop resolved entries from the manifest       |
| `--xml-in/-out` | relocate_rekordbox      | Rekordbox XML paths (env: `REKORDBOX_XML*`)   |

### Auditing lossy-sourced files

Files created by `acquire_missing` are copies of a *lossy* source. They are
tracked in `lossy_sourced_manifest.json` (produced FLAC size/mtime + audio
sample count). `check_lossy` reports which are still lossy and which have been
replaced by a real rip:

```sh
.venv/bin/python main.py -t check_lossy             # report only
.venv/bin/python main.py -t check_lossy --prune     # drop resolved entries
```

Replacement is detected by comparing the audio sample count (a metadata-only
edit does not count as a replacement). Replace a file by decoding a real
lossless source over it, then re-running `check_lossy --prune`.

## Rolling back / safety

* Nothing here deletes audio silently: `normalize_names` renames (report in
  `rename_report.txt`), `sync_library`/`acquire_missing` only *add* files.
  `prune_empty_dirs` only removes empty folders.
* `find_missing_lossless` and `check_lossy` are read-only.
* The Rekordbox output is a *new* XML (`rekordbox_aiff.xml`) — the original is
  never overwritten.
* Keep a tree backup (or the git history if the trees live under version
  control) before the first big run; `rename_report.txt` gives you the exact
  source → destination pairs to reverse any move.

## Testing

Point the whole suite at a throwaway tree without touching the NAS:

```sh
export MUSIC_ROOT="$TMPDIR/opencode/sbx/music"
.venv/bin/python main.py -t pipeline
```

The `tests/` folder contains repeatable fixture builders used during
development:

* `_sandbox_make_rekordbox_xml.py` – builds a `test_rekordbox.xml` from the
  (sandbox) mp3 tree, including an already-AIFF track, a zombie and an
  outside-root track for tag lookup.
* `_sandbox_relo_test.py` – a minimal three-tree fixture with controlled
  durations that exercises the relocation strategies, including the
  `same_audio` duration-rejection path.

## Notes / gotchas

* **AIFF tags**: always go through `mutagen.aiff.AIFF`; a plain `ID3()` read/write
  would prepend a tag block and corrupt the file. Tags are written as ID3v2.3
  (what CDJs expect); `update_to_v23()` is applied before saving so dates
  round-trip.
* **FAT32-invalid characters** (`<>:"|?*`) appear in a handful of folder names
  (`Pan:Tone`, `Day:Night`); `normalize_names` replaces them and mirrors the
  change across all three trees.
* **MusicBrainz terms**: the client sends a proper `User-Agent` and throttles
  to one request per second; do not run `scrape_metadata` with concurrency or
  against a robot-strained key.
* **Loose-track buckets** (`Misc/`, `YouTube Download/`, `No Album`, …) get a
  per-track *recording* lookup instead of an album search and never get an
  invented album name.
* **MP3-only multi-disc albums** (the Buddha-Bar compilations, …) have no FLAC
  twin, so `normalize_names` does not touch them yet — they keep their CD
  sub-folders until a FLAC twin exists (`find_missing_lossless` lists them).
* Track titles adopted from MusicBrainz may differ cosmetically from the current
  file names (feat. credits dropped, curly apostrophes, capitalization), and
  lowercase renames change every file/folder name once. Both are intended: the
  file name follows the canonical tag. Re-running `relocate_rekordbox` against
  the *original* export keeps working after rename-heavy runs: every rename is
  traced through `rename_history.txt`, so the current AIFF path is found for all
  renamed tracks. Anything still unmatched lands in `rekordbox_unmatched.txt`
  for manual review.