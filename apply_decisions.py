#!/usr/bin/env python3
"""
Apply PII review decisions to the finetune dataset.

Workflow:
  1. Open summary.html in a browser, mark decisions, click "Download Decisions CSV"
  2. Run: python3.12 apply_decisions.py --decisions decisions.csv --output /path/to/output

For each flag marked "restore", this script:
  - Finds the source document (via provenance.json)
  - Re-runs PII stripping on that document, excluding the restored entities
  - Patches the corresponding record in finetune/dataset.jsonl
  - Regenerates summary.html and review_log.csv
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from cleaner import clean
from chunker import chunk, count_tokens
from pii import strip_pii
from reader import read_file
from classifier import classify
from reporter import generate_reports


def load_decisions(decisions_path: Path) -> tuple[list, list]:
    """Returns (restores, approves) as lists of decision dicts."""
    restores, approves = [], []
    with open(decisions_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            (restores if row.get('decision','').strip().lower() == 'restore' else approves).append(row)
    return restores, approves


def load_provenance(output_dir: Path) -> dict:
    path = output_dir / 'provenance.json'
    if not path.exists():
        print(f"Error: provenance.json not found in {output_dir}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def build_exclusion_map(restores: list) -> dict:
    """Returns {filename: set(original_text)} — entities to NOT redact."""
    exclusions = defaultdict(set)
    for r in restores:
        exclusions[r['file']].add(r['original_text'])
    return dict(exclusions)


def reprocess_file(source_path: Path, exclusions: set, output_dir: Path,
                   provenance_rec: dict) -> dict:
    """Re-run PII stripping on one file with exclusions. Returns updated metadata."""
    print(f"  Re-processing: {source_path.name}")

    read_result = read_file(source_path)
    if read_result is None:
        print(f"    WARNING: could not read {source_path.name} — skipping")
        return provenance_rec

    text = clean(read_result.text)

    # Run PII stripping with exclusions
    from presidio_analyzer import Pattern, PatternRecognizer
    from pii import _get_engines, _analyze_chunked, _faker_replace, _token_replace
    from pii import HIGH_CONFIDENCE, LOW_CONFIDENCE

    analyzer, _ = _get_engines()
    all_results = _analyze_chunked(analyzer, text)

    # Filter out excluded entity texts
    all_results = [
        r for r in all_results
        if text[r.start:r.end] not in exclusions
    ]

    high = [r for r in all_results if r.score >= HIGH_CONFIDENCE]
    medium = [r for r in all_results if LOW_CONFIDENCE <= r.score < HIGH_CONFIDENCE]
    to_redact = high + medium

    review_flags = []
    for r in medium:
        ctx_start = max(0, r.start - 60)
        ctx_end = min(len(text), r.end + 60)
        review_flags.append({
            'file': source_path.name,
            'entity_type': r.entity_type,
            'original_text': text[r.start:r.end],
            'confidence': round(r.score, 3),
            'context': text[ctx_start:ctx_end],
            'action': 'redacted_pending_review',
        })

    if to_redact:
        text = _faker_replace(text, to_redact)

    chunks = chunk(text, provenance_rec.get('doc_type', 'private'))
    token_count = sum(c.token_count for c in chunks)

    # Update finetune record
    anon_id = f"anon_{abs(hash(source_path.name)):08d}"
    _patch_finetune(output_dir, anon_id, text, provenance_rec.get('doc_type', 'private'),
                    len(to_redact), len(review_flags), token_count)

    # Update review log (remove old flags for this file, add new ones)
    _patch_review_log(output_dir, source_path.name, review_flags)

    # Update provenance record
    provenance_rec = dict(provenance_rec)
    provenance_rec['processing'] = dict(provenance_rec.get('processing', {}))
    provenance_rec['processing']['faker_substitutions'] = len(to_redact)
    provenance_rec['processing']['review_flags'] = len(review_flags)
    provenance_rec['processing']['token_count'] = token_count
    return provenance_rec


def _patch_finetune(output_dir: Path, anon_id: str, text: str,
                    doc_type: str, faker_subs: int, review_flags: int, token_count: int) -> None:
    finetune_path = output_dir / 'finetune' / 'dataset.jsonl'
    if not finetune_path.exists():
        return

    records = []
    found = False
    with open(finetune_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('metadata', {}).get('source') == anon_id:
                rec['text'] = text
                rec['metadata']['faker_substitutions'] = faker_subs
                rec['metadata']['review_flags'] = review_flags
                rec['metadata']['token_count'] = token_count
                found = True
            records.append(rec)

    if not found:
        print(f"    WARNING: no finetune record found for {anon_id}")
        return

    with open(finetune_path, 'w') as f:
        for rec in records:
            f.write(json.dumps(rec) + '\n')


def _patch_review_log(output_dir: Path, filename: str, new_flags: list) -> None:
    log_path = output_dir / 'review' / 'review_log.jsonl'
    if not log_path.exists():
        return

    # Remove old flags for this file, append new ones
    kept = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if rec.get('file') != filename:
                    kept.append(rec)

    kept.extend(new_flags)

    with open(log_path, 'w') as f:
        for rec in kept:
            f.write(json.dumps(rec) + '\n')


def _write_audit_record(output_dir: Path, decisions_src: Path,
                         approves: list, restores: list) -> Path:
    """
    Archive the decisions CSV into output/review/ and append a human_review
    entry to provenance.json. Returns the path of the archived decisions file.
    """
    from datetime import datetime, timezone
    import shutil

    timestamp = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    # Archive the decisions file into the output directory
    review_dir = output_dir / 'review'
    review_dir.mkdir(parents=True, exist_ok=True)
    archived = review_dir / f'decisions-{stamp}.csv'
    shutil.copy2(decisions_src, archived)

    # Append human_review block to provenance.json
    provenance_path = output_dir / 'provenance.json'
    provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else {}

    review_record = {
        'reviewed_at': timestamp,
        'decisions_file': archived.name,
        'total_flags_reviewed': len(approves) + len(restores),
        'approved_keep_redacted': len(approves),
        'restored_original': len(restores),
    }

    if 'human_reviews' not in provenance:
        provenance['human_reviews'] = []
    provenance['human_reviews'].append(review_record)

    provenance_path.write_text(json.dumps(provenance, indent=2))

    print(f"\nAudit record written:")
    print(f"  Decisions archived: {archived}")
    print(f"  provenance.json updated with human_review entry")
    print(f"  Reviewed at: {timestamp}")
    print(f"  {len(approves)} approved (kept redacted), {len(restores)} restored")

    return archived


def main():
    parser = argparse.ArgumentParser(
        description='Apply PII review decisions to the finetune dataset.'
    )
    parser.add_argument('--decisions', required=True, type=Path,
                        help='decisions.csv downloaded from summary.html')
    parser.add_argument('--output', required=True, type=Path,
                        help='Pipeline output directory (contains provenance.json)')
    parser.add_argument('--reviewer', default='', type=str,
                        help='Name of reviewer for audit trail (optional)')
    args = parser.parse_args()

    if not args.decisions.exists():
        print(f"Error: {args.decisions} not found", file=sys.stderr)
        sys.exit(1)

    restores, approves = load_decisions(args.decisions)
    print(f"Decisions: {len(approves)} keep-redacted, {len(restores)} restore")

    # Always write the audit record — even if no restores
    _write_audit_record(args.output, args.decisions, approves, restores)

    if not restores:
        print("\nNo entities to restore — dataset unchanged.")
        print("Audit record written to provenance.json and review/ directory.")
        return

    exclusion_map = build_exclusion_map(restores)
    print(f"Re-processing {len(exclusion_map)} file(s) with restored entities...")

    provenance = load_provenance(args.output)

    updated_records = []
    for rec in provenance.get('files', []):
        fname = rec['original_filename']
        if fname in exclusion_map:
            source_path = Path(rec.get('source_path', ''))
            if not source_path.exists():
                print(f"  WARNING: source file not found: {source_path} — skipping {fname}")
                updated_records.append(rec)
                continue
            rec = reprocess_file(source_path, exclusion_map[fname], args.output, rec)
        updated_records.append(rec)

    # Save updated provenance
    provenance['files'] = updated_records
    total_flags = sum(
        r.get('processing', {}).get('review_flags', 0)
        for r in updated_records if not r.get('skipped')
    )
    provenance['summary']['pii_review_flags'] = total_flags

    # Stamp the last restore reviewer if provided
    if args.reviewer and provenance.get('human_reviews'):
        provenance['human_reviews'][-1]['reviewer'] = args.reviewer

    (args.output / 'provenance.json').write_text(json.dumps(provenance, indent=2))

    print("Regenerating reports...")
    generate_reports(args.output)
    print(f"\nDone. {len(restores)} entities restored across {len(exclusion_map)} file(s).")
    print(f"Open {args.output}/summary.html to review the updated report.")


if __name__ == '__main__':
    main()
