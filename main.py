from audio_converter.flac_to_mp3 import sync_library
from audio_converter.split_cue import split_flac_by_cue
from argparse import ArgumentParser


def parse_args():
    parser = ArgumentParser()

    parser.add_argument("--task", "-t", type=str, default="sync_library")
    parser.add_argument("--cue_path", type=str)
    parser.add_argument("--flac_path", type=str)

    return parser.parse_args()


def main(args):
    if args.task == "sync_library":
        sync_library()
    elif args.task == "split_flac":
        split_flac_by_cue(args.cue_path, args.flac_path)


if __name__ == "__main__":
    args = parse_args()
    main(args)
