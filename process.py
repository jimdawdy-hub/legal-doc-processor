#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from pipeline import process_directory
from utils import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Convert legal documents to AI/LLM-ready JSONL datasets.'
    )
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--input',   required=True, type=Path, help='Input directory')
    parser.add_argument('--output',  required=True, type=Path, help='Output directory')
    parser.add_argument('--dry-run', action='store_true',
                        help='Classify files only; write nothing')
    parser.add_argument('--workers', type=int, default=1,
                        help='Parallel worker processes (default: 1)')
    parser.add_argument('--sidecar', type=Path, default=None,
                        help='CSV with copyright/source info per file')
    parser.add_argument('--preserve-public-text', action='store_true',
                        help='Opt out of redaction for classified public sources; verify they contain no private material')
    parser.add_argument('--replacement', choices=['tokens', 'synthetic'], default='tokens',
                        help='Replacement style (default: typed tokens; synthetic values are not real facts)')
    args = parser.parse_args()

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
        preserve_public_text=args.preserve_public_text,
        replacement=args.replacement,
    )


if __name__ == '__main__':
    main()
