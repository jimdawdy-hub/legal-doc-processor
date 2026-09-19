#!/usr/bin/env python3
"""
Read-only viewer for a batch's verification record, and the accept action.

The pipeline needs no per-document approval (R13). This tool exists so a human
can check that a batch was scrubbed correctly, and then say so -- which is what
deletes the original values it holds (R18).

It is the one consumer that legitimately reads original identifier values, so
it is also the one place the accept action belongs.

Usage:
    python3.12 review_pii.py --list
    python3.12 review_pii.py --batch <batch-id> --summary
    python3.12 review_pii.py --batch <batch-id> --type US_SSN
    python3.12 review_pii.py --batch <batch-id> --csv ~/review.csv
    python3.12 review_pii.py --accept <batch-id>
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from writer import accept_batch, records_root, unaccepted_batches


def load_identifiers(batch_id: str) -> list:
    """Every recorded identifier in a batch, with its original value."""
    batch_dir = records_root() / 'verification' / batch_id
    if not batch_dir.is_dir():
        print(f"Error: no verification record for batch {batch_id!r}", file=sys.stderr)
        print(f"Looked in {batch_dir}", file=sys.stderr)
        sys.exit(1)

    found = []
    for record_path in sorted(batch_dir.glob('*.json')):
        if record_path.name == 'batch.json':
            continue
        record = json.loads(record_path.read_text())
        for entry in record.get('identifiers', []):
            found.append({**entry, 'anon_id': record.get('anon_id', '')})
    return found


def print_batches() -> None:
    batches = unaccepted_batches()
    if not batches:
        print("No batches are holding original identifier values.")
        return
    print(f"\n{len(batches)} batch(es) still hold original identifier values:\n")
    for info in batches:
        print(f"  {info['batch_id']}")
        print(f"      opened   {info.get('opened_at', 'unknown')}")
        print(f"      output   {info.get('output_dir', 'unknown')}")
        print(f"      records  {info.get('record_count', 0)} documents")
    print("\nAccept a batch once you have checked it, to delete its values:")
    print("  python3.12 review_pii.py --accept <batch-id>\n")


def print_summary(identifiers: list, batch_id: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  VERIFICATION RECORD — batch {batch_id}")
    print(f"  {len(identifiers)} recorded identifiers")
    print(f"{'=' * 60}")

    by_doc = defaultdict(list)
    for entry in identifiers:
        by_doc[entry['anon_id']].append(entry)
    by_type = Counter(entry['entity_type'] for entry in identifiers)

    print("\nBy identifier type:")
    for entity_type, count in by_type.most_common():
        print(f"  {entity_type:<25} {count:>5}")

    print(f"\nBy document ({len(by_doc)} documents):")
    for anon_id, entries in sorted(by_doc.items(), key=lambda kv: -len(kv[1])):
        types = Counter(e['entity_type'] for e in entries)
        detail = ', '.join(f"{t}:{n}" for t, n in types.most_common(3))
        print(f"  {len(entries):>5}  {anon_id}")
        print(f"         [{detail}]")

    redacted = sum(1 for e in identifiers if e.get('redacted'))
    print(f"\nRedacted: {redacted} of {len(identifiers)}"
          f"  ({len(identifiers) - redacted} recorded below the floor and left in place)")
    print()


def print_identifiers(identifiers: list, limit: int = None) -> None:
    shown = identifiers[:limit] if limit else identifiers
    for i, entry in enumerate(shown, 1):
        print(f"\n{'-' * 60}")
        print(f"[{i}/{len(identifiers)}] {entry['entity_type']}  "
              f"score={entry['score']:.2f}  "
              f"{'redacted' if entry.get('redacted') else 'LEFT IN PLACE'}")
        print(f"Document: {entry['anon_id']}")
        print(f"Offset:   {entry['start']}-{entry['end']}")
        print(f"Value:    \"{entry.get('original_value')}\"")
    if limit and len(identifiers) > limit:
        print(f"\n  (showing {limit} of {len(identifiers)} — use --csv to export all)")


def _refuse_export_inside_output(out_path: Path) -> None:
    """A CSV from this tool carries original identifier values.

    Dropping the export next to the deliverable is exactly how those values end
    up in the directory that leaves the machine (R10). The habit is the risk,
    so the tool refuses rather than warns.
    """
    resolved = out_path.resolve()
    for candidate in (resolved, *resolved.parents):
        for marker in ('provenance.json', 'summary.html'):
            if (candidate / marker).exists():
                print(f"Error: {out_path} is inside an output directory "
                      f"({candidate}).", file=sys.stderr)
                print("This export contains original identifier values, and "
                      "nothing in an output directory may.", file=sys.stderr)
                print("Write it somewhere outside that directory instead.",
                      file=sys.stderr)
                sys.exit(1)
        if (candidate / 'rag').is_dir() and (candidate / 'finetune').is_dir():
            print(f"Error: {out_path} is inside an output directory "
                  f"({candidate}).", file=sys.stderr)
            sys.exit(1)


def export_csv(identifiers: list, out_path: Path) -> None:
    _refuse_export_inside_output(out_path)
    fieldnames = ['anon_id', 'entity_type', 'score', 'start', 'end',
                  'original_value', 'redacted']
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(identifiers)
    out_path.chmod(0o600)
    print(f"Exported {len(identifiers)} identifiers to {out_path}")
    print("This file contains original identifier values. Delete it when you "
          "have finished checking the batch.")


def main():
    parser = argparse.ArgumentParser(
        description="Read a batch's verification record, then accept the batch."
    )
    parser.add_argument('--list', action='store_true',
                        help='List batches still holding original values')
    parser.add_argument('--batch', default=None, help='Batch id to read')
    parser.add_argument('--accept', default=None, metavar='BATCH_ID',
                        help='Accept a batch and delete its original values')
    parser.add_argument('--csv', type=Path, default=None,
                        help='Export to CSV (must be outside any output directory)')
    parser.add_argument('--type', default=None, dest='entity_type',
                        help='Filter to one identifier type (e.g. US_SSN)')
    parser.add_argument('--document', default=None,
                        help='Filter to one document by anonymous id (partial match)')
    parser.add_argument('--summary', action='store_true',
                        help='Show summary statistics only')
    parser.add_argument('--show', type=int, default=20,
                        help='Individual identifiers to print (default 20, 0 = all)')
    args = parser.parse_args()

    if args.accept:
        removed = accept_batch(args.accept)
        print(f"Accepted batch {args.accept}: deleted {removed} verification "
              f"record(s) holding original values.")
        print("The evidence record -- types, counts and locations -- is kept.")
        return

    if args.list or not args.batch:
        print_batches()
        if not args.batch:
            return

    identifiers = load_identifiers(args.batch)

    if args.document:
        identifiers = [e for e in identifiers
                       if args.document.lower() in e['anon_id'].lower()]
    if args.entity_type:
        identifiers = [e for e in identifiers
                       if e['entity_type'].upper() == args.entity_type.upper()]

    if not identifiers:
        print("No recorded identifiers match the filter.")
        return

    print_summary(identifiers, args.batch)

    if args.csv:
        export_csv(identifiers, args.csv)
    elif not args.summary:
        limit = None if args.show == 0 else args.show
        print_identifiers(identifiers, limit=limit)


if __name__ == '__main__':
    main()
