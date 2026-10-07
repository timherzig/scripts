from argparse import ArgumentParser

from audio_converter.acquire_missing import acquire_missing, check_lossy_manifest
from audio_converter.find_missing_lossless import find_missing_lossless
from audio_converter.flac_to_mp3 import sync_library
from audio_converter.normalize_names import normalize_names
from audio_converter.relocate_rekordbox import relocate_rekordbox_xml
from audio_converter.scrape_metadata import scrape_metadata
from audio_converter.split_cue import split_flac_by_cue
from audio_converter.sync_tags import sync_tags

# The full migration, in dependency order. Each step is idempotent and can also
# be run on its own with --task <name>.
PIPELINE = (
    "acquire_missing",     # mp3 -> flac + aiff for tracks with no lossless twin
    "scrape_metadata",     # MusicBrainz tags + cover art onto the flac tree
    "sync_tags",           # push flac tags/covers to mp3 + aiff, align mtimes
    "normalize_names",     # one canonical file/folder name across all trees
    "sync_library",        # convert any remaining missing/stale mp3 + aiff
    "find_missing_lossless",  # verify the trees are identical
    "relocate_rekordbox",  # repoint the rekordbox XML at the aiff tree
)


def parse_args():
    parser = ArgumentParser(
        description="FLAC/AIFF/MP3 library maintenance and Rekordbox migration."
    )

    parser.add_argument("--task", "-t", type=str, default="pipeline",
                        choices=list(PIPELINE) + ["pipeline", "sync_library",
                                                 "split_flac", "check_lossy"],
                        help="Task to run (default: full pipeline)")
    parser.add_argument("--cue_path", type=str)
    parser.add_argument("--flac_path", type=str)

    # scoping / behaviour flags
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N albums/files (for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only show what normalize_names would rename")
    parser.add_argument("--force", action="store_true",
                        help="Re-query MusicBrainz even if the album is cached")
    parser.add_argument("--force-covers", action="store_true",
                        help="Re-download covers even when one exists")
    parser.add_argument("--prune", action="store_true",
                        help="With check_lossy: drop resolved entries from the manifest")
    parser.add_argument("--no-flatten-discs", action="store_true",
                        help="normalize_names: keep CD1/CD2 sub-folders instead of "
                             "folding multi-disc albums into one folder")
    parser.add_argument("--no-lowercase", action="store_true",
                        help="normalize_names: keep Mixed Case file and folder names")

    # rekordbox xml locations
    parser.add_argument("--xml-in", type=str, default=None,
                        help="Input rekordbox XML (default: ~/Desktop/rekordbox.xml)")
    parser.add_argument("--xml-out", type=str, default=None,
                        help="Output rekordbox XML (default: ~/Desktop/rekordbox_aiff.xml)")

    return parser.parse_args()


def run_task(task: str, args) -> None:
    if task == "sync_library":
        sync_library()
    elif task == "split_flac":
        split_flac_by_cue(args.cue_path, args.flac_path)
    elif task == "acquire_missing":
        acquire_missing(limit=args.limit)
    elif task == "scrape_metadata":
        scrape_metadata(limit=args.limit, force=args.force, force_covers=args.force_covers)
    elif task == "sync_tags":
        sync_tags(limit=args.limit, replace_cover=args.force_covers)
    elif task == "normalize_names":
        normalize_names(limit=args.limit, dry_run=args.dry_run,
                        flatten_discs=not args.no_flatten_discs,
                        lowercase=not args.no_lowercase)
    elif task == "find_missing_lossless":
        find_missing_lossless()
    elif task == "relocate_rekordbox":
        relocate_rekordbox_xml(xml_in=args.xml_in, xml_out=args.xml_out)
    elif task == "check_lossy":
        check_lossy_manifest(prune=args.prune)
    else:
        raise SystemExit(f"Unknown task: {task}")


def main(args):
    if args.task == "pipeline":
        for i, task in enumerate(PIPELINE, start=1):
            print(f"\n=== [{i}/{len(PIPELINE)}] {task} ===")
            run_task(task, args)
        print("\n=== Pipeline complete ===")
    else:
        run_task(args.task, args)


if __name__ == "__main__":
    main(parse_args())
