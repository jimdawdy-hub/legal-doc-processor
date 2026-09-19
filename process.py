#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path
from pipeline import legacy_output_directories, process_directory


def _find_legacy(search_root: Path, delete: bool) -> None:
    """Locate output directories produced before source identities were evicted.

    Their provenance entries carry real filenames and full source paths -- the
    exact leak this version closes for new runs. Regenerating over such a
    directory does not clear it; the old entries have to go.
    """
    found = legacy_output_directories([search_root])
    if not found:
        print(f"No pre-eviction output directories under {search_root}.")
        return

    print(f"\n{len(found)} output director(y/ies) under {search_root} carry "
          f"source identities:\n")
    for directory in found:
        contents = sorted(p.name for p in directory.iterdir())
        print(f"  {directory}")
        print(f"      contains: {', '.join(contents[:8])}"
              f"{' ...' if len(contents) > 8 else ''}")

    if not delete:
        print("\nThese are not regenerable into a clean state -- the old "
              "provenance entries have to be removed.")
        print("Review the list above, then re-run with --delete-legacy-output "
              "to remove them.")
        return

    print("\nThis permanently deletes the directories listed above.")
    confirmation = input("Type DELETE to confirm: ").strip()
    if confirmation != 'DELETE':
        print("Cancelled. Nothing was removed.")
        return
    for directory in found:
        shutil.rmtree(directory)
        print(f"  removed {directory}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Convert legal documents to AI/LLM-ready JSONL datasets.'
    )
    parser.add_argument('--find-legacy-output', type=Path, default=None,
                        metavar='DIR',
                        help='Find output directories that still carry source '
                             'filenames and paths, and report them')
    parser.add_argument('--delete-legacy-output', action='store_true',
                        help='With --find-legacy-output, delete them after '
                             'an explicit typed confirmation')
    parser.add_argument('--input',   type=Path, help='Input directory')
    parser.add_argument('--output',  type=Path, help='Output directory')
    parser.add_argument('--dry-run', action='store_true',
                        help='Classify files only; write nothing')
    parser.add_argument('--workers', type=int, default=1,
                        help='Parallel worker processes (default: 1)')
    parser.add_argument('--sidecar', type=Path, default=None,
                        help='CSV with copyright/source info per file')
    args = parser.parse_args()

    if args.find_legacy_output:
        _find_legacy(args.find_legacy_output, args.delete_legacy_output)
        return

    if not args.input or not args.output:
        parser.error('--input and --output are required')
    if not args.input.is_dir():
        print(f"Error: {args.input} is not a directory", file=sys.stderr)
        sys.exit(1)
    if args.sidecar and not args.sidecar.exists():
        print(f"Error: sidecar {args.sidecar} not found", file=sys.stderr)
        sys.exit(1)

    process_directory(
        input_dir=args.input,
        output_dir=args.output,
        dry_run=args.dry_run,
        workers=args.workers,
        sidecar_path=args.sidecar,
    )


if __name__ == '__main__':
    main()
