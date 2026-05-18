#!/usr/bin/env python3
"""
Review PII flags from the pipeline's review_log.jsonl.

Usage:
    python3.12 review_pii.py --log /path/to/review_log.jsonl
    python3.12 review_pii.py --log /path/to/review_log.jsonl --csv /path/to/output.csv
    python3.12 review_pii.py --log /path/to/review_log.jsonl --file "brief_001.pdf"
    python3.12 review_pii.py --log /path/to/review_log.jsonl --type PERSON
    python3.12 review_pii.py --log /path/to/review_log.jsonl --summary
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_flags(log_path: Path) -> list:
    flags = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                flags.append(json.loads(line))
    return flags


def print_summary(flags: list) -> None:
    print(f"\n{'='*60}")
    print(f"  PII REVIEW SUMMARY — {len(flags)} flagged entities")
    print(f"{'='*60}")

    by_file = defaultdict(list)
    for f in flags:
        by_file[f['file']].append(f)

    by_type = Counter(f['entity_type'] for f in flags)

    print(f"\nBy entity type:")
    for etype, count in by_type.most_common():
        print(f"  {etype:<25} {count:>5}")

    print(f"\nBy source file ({len(by_file)} files with flags):")
    for fname, fflags in sorted(by_file.items(), key=lambda x: -len(x[1])):
        types = Counter(f['entity_type'] for f in fflags)
        type_str = ', '.join(f"{t}:{n}" for t, n in types.most_common(3))
        print(f"  {len(fflags):>5} flags  {fname}")
        print(f"           [{type_str}]")

    lo = min(f['confidence'] for f in flags)
    hi = max(f['confidence'] for f in flags)
    avg = sum(f['confidence'] for f in flags) / len(flags)
    print(f"\nConfidence range: {lo:.2f} – {hi:.2f}  (avg {avg:.2f})")
    print(f"\nAll {len(flags)} entities were REDACTED with Faker replacements.")
    print("Review these if you want to verify the redaction was appropriate.")
    print()


def print_flags(flags: list, limit: int = None) -> None:
    shown = flags[:limit] if limit else flags
    for i, f in enumerate(shown, 1):
        print(f"\n{'─'*60}")
        print(f"[{i}/{len(flags)}] {f['entity_type']}  confidence={f['confidence']:.2f}")
        print(f"File: {f['file']}")
        print(f"Detected text: \"{f['original_text']}\"")
        print(f"Context: ...{f['context']}...")
    if limit and len(flags) > limit:
        print(f"\n  (showing {limit} of {len(flags)} — use --csv to export all)")


def export_csv(flags: list, out_path: Path) -> None:
    fieldnames = ['file', 'entity_type', 'confidence', 'original_text', 'context', 'action']
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(flags)
    print(f"Exported {len(flags)} flags to {out_path}")
    print("Open in any spreadsheet. Sort by 'file' or 'entity_type' to work through them.")


def main():
    parser = argparse.ArgumentParser(description='Review PII flags from the legal-doc-processor.')
    parser.add_argument('--log', required=True, type=Path,
                        help='Path to review_log.jsonl')
    parser.add_argument('--csv', type=Path, default=None,
                        help='Export flags to a CSV file for spreadsheet review')
    parser.add_argument('--file', default=None,
                        help='Filter to flags from a specific source file (partial match)')
    parser.add_argument('--type', default=None, dest='entity_type',
                        help='Filter to a specific entity type (e.g. PERSON, US_SSN, EMAIL_ADDRESS)')
    parser.add_argument('--summary', action='store_true',
                        help='Show summary statistics only (no individual flags)')
    parser.add_argument('--show', type=int, default=20,
                        help='Number of individual flags to print (default 20, 0 = all)')
    args = parser.parse_args()

    if not args.log.exists():
        print(f"Error: {args.log} not found", file=sys.stderr)
        sys.exit(1)

    flags = load_flags(args.log)

    if args.file:
        flags = [f for f in flags if args.file.lower() in f['file'].lower()]
        print(f"Filtered to '{args.file}': {len(flags)} flags")

    if args.entity_type:
        flags = [f for f in flags if f['entity_type'].upper() == args.entity_type.upper()]
        print(f"Filtered to {args.entity_type}: {len(flags)} flags")

    if not flags:
        print("No flags match the filter.")
        return

    print_summary(flags)

    if args.csv:
        export_csv(flags, args.csv)
    elif not args.summary:
        limit = None if args.show == 0 else args.show
        print_flags(flags, limit=limit)
        if limit and len(flags) > limit:
            print(f"\nTip: use --csv output.csv to export all {len(flags)} for spreadsheet review.")


if __name__ == '__main__':
    main()
