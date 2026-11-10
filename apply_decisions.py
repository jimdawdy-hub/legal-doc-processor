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
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from cleaner import clean
from chunker import chunk
from pii import _get_engines, _analyze_chunked, _faker_replace
from pii import HIGH_CONFIDENCE, LOW_CONFIDENCE
from reader import read_file
from utils import document_id, atomic_text, write_json, sha256_file, csv_safe
from reporter import generate_reports


def load_decisions(decisions_path: Path) -> tuple[list, list]:
    """Returns (restores, approves) as lists of decision dicts."""
    restores, approves = [], []
    with open(decisions_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            decision = row.get('decision', '').strip().lower()
            if decision not in {'approve', 'restore'}:
                raise ValueError('Each decision must be approve or restore')
            row['decision'] = decision
            (restores if decision == 'restore' else approves).append(row)
    return restores, approves


def load_provenance(output_dir: Path) -> dict:
    path = output_dir / 'provenance.json'
    if not path.exists():
        raise ValueError('provenance.json is missing; regenerate the batch')
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
        raise ValueError('Source no longer passes the OCR quality gate')

    text = clean(read_result.text)

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

    from pii import _token_replace
    if to_redact:
        text = (_faker_replace(text, to_redact) if provenance_rec.get('processing', {}).get('replacement') == 'synthetic'
                else _token_replace(text, to_redact))

    chunks = chunk(text, provenance_rec.get('doc_type', 'private'))
    token_count = sum(c.token_count for c in chunks)

    # Update finetune record
    anon_id = provenance_rec.get('source_id', document_id(source_path))
    _patch_finetune(output_dir, anon_id, text, provenance_rec.get('doc_type', 'private'),
                    len(to_redact), len(review_flags), token_count)

    # Update review log (remove old flags for this file, add new ones)
    for flag in review_flags:
        flag['file'] = anon_id
    _patch_review_log(output_dir, anon_id, review_flags)

    # Update provenance record
    provenance_rec = dict(provenance_rec)
    provenance_rec['processing'] = dict(provenance_rec.get('processing', {}))
    provenance_rec['processing']['faker_substitutions'] = len(to_redact)
    provenance_rec['processing']['review_flags'] = len(review_flags)
    provenance_rec['processing']['token_count'] = token_count
    provenance_rec['processing']['chunk_count'] = len(chunks)
    return provenance_rec


def _patch_finetune(output_dir: Path, anon_id: str, text: str,
                    doc_type: str, faker_subs: int, review_flags: int, token_count: int) -> None:
    finetune_path = output_dir / 'finetune' / 'dataset.jsonl'
    if not finetune_path.exists():
        raise ValueError('Analysis dataset is missing; regenerate the batch')

    records = [json.loads(line) for line in finetune_path.read_text().splitlines() if line.strip()]
    if not any(r.get('metadata', {}).get('source') == anon_id for r in records):
        raise ValueError('No matching dataset ID; regenerate legacy batches before restoring PII')
    patched, inserted = [], False
    for record in records:
        if record.get('metadata', {}).get('source') != anon_id:
            patched.append(record)
            continue
        if not inserted:
            for part in chunk(text, doc_type):
                meta = dict(record['metadata'])
                meta.update(faker_substitutions=faker_subs, review_flags=review_flags,
                            token_count=part.token_count, chunk_index=part.index, total_chunks=part.total)
                patched.append({'text': part.text, 'metadata': meta})
            inserted = True
    with atomic_text(finetune_path) as stream:
        for record in patched:
            stream.write(json.dumps(record) + '\n')
    # The same document may also have RAG chunks. Restore/redact both exports.
    rag = output_dir / 'rag' / (anon_id + '.jsonl')
    if rag.exists():
        from writer import write_rag_chunks
        existing = json.loads(rag.read_text().splitlines()[0])['metadata']
        extra = {k: v for k, v in existing.items() if k not in {'source', 'doc_type', 'chunk_index', 'total_chunks', 'token_count'}}
        write_rag_chunks(chunk(text, doc_type), anon_id, doc_type, extra, output_dir / 'rag')


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

    with atomic_text(log_path) as f:
        for rec in kept:
            f.write(json.dumps(rec) + '\n')


def validate_decisions(output_dir, decisions):
    if not isinstance(decisions, list) or len(decisions) > 100_000:
        raise ValueError('Invalid decisions list')
    log = output_dir / 'review' / 'review_log.jsonl'
    flags = [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []
    seen = set()
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get('decision') not in {'approve', 'restore'}:
            raise ValueError('Invalid decision')
        raw_id = decision.get('flag_id')
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            raise ValueError('Invalid flag ID')
        index = int(raw_id)
        if index in seen or not 0 <= index < len(flags):
            raise ValueError('Unknown or duplicate flag ID')
        seen.add(index)
        for key in ['file', 'entity_type', 'original_text']:
            expected = flags[index].get(key)
            if decision.get(key) not in (expected, csv_safe(expected)):
                raise ValueError('Decision no longer matches the review log; refresh the report')
            decision[key] = expected
    return decisions


def apply_review(output_dir, decisions, reviewer):
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        raise ValueError('Reviewer name is required (maximum 200 characters)')
    validate_decisions(output_dir, decisions)
    restores = [d for d in decisions if d['decision'] == 'restore']
    approves = [d for d in decisions if d['decision'] == 'approve']
    provenance = load_provenance(output_dir)
    if restores and provenance.get('second_pass_redactions'):
        raise ValueError('Restoring would undo second-pass redactions; regenerate and reassess instead')
    exclusions = build_exclusion_map(restores)
    matched = {}
    for rec in provenance.get('files', []):
        key = rec.get('source_id', rec.get('original_filename'))
        if key in exclusions:
            source = Path(rec['source_path'])
            if not source.is_file() or sha256_file(source) != rec.get('sha256'):
                raise ValueError('Source is missing or changed; no restore was applied')
            matched[key] = (source, rec)
    if set(matched) != set(exclusions):
        raise ValueError('Restore does not match the batch provenance')
    if matched:
        # Finish every re-analysis in a private staging directory before any
        # export is replaced. NLP/read failures cannot leave a partial restore.
        with tempfile.TemporaryDirectory(prefix='.review-stage-', dir=output_dir.parent) as temporary:
            stage = Path(temporary)
            files = [output_dir / 'finetune/dataset.jsonl', output_dir / 'review/review_log.jsonl']
            files += [output_dir / 'rag' / (key + '.jsonl') for key in matched]
            for original in files:
                if original.exists():
                    target = stage / original.relative_to(output_dir)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(original, target)
            updated = []
            for key, (source, rec) in matched.items():
                updated.append((rec, reprocess_file(source, exclusions[key], stage, rec)))
            for staged in stage.rglob('*.jsonl'):
                with staged.open(encoding='utf-8') as source, atomic_text(output_dir / staged.relative_to(stage)) as target:
                    shutil.copyfileobj(source, target)
            for original, replacement in updated:
                original.update(replacement)
    provenance.setdefault('summary', {})['pii_review_flags'] = sum(
        rec.get('processing', {}).get('review_flags', 0) for rec in provenance.get('files', []))
    write_json(output_dir / 'provenance.json', provenance)
    review_dir = output_dir / 'review'
    review_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    archived = review_dir / ('decisions-' + stamp + '.csv')
    with atomic_text(archived) as stream:
        fields = ['flag_id', 'file', 'entity_type', 'original_text', 'confidence', 'decision']
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows({key: csv_safe(value) for key, value in decision.items()} for decision in decisions)
    provenance.setdefault('human_reviews', []).append({
        'reviewed_at': datetime.now(timezone.utc).isoformat(), 'reviewer': reviewer.strip(),
        'decisions_file': archived.name, 'total_flags_reviewed': len(decisions),
        'approved_keep_redacted': len(approves), 'restored_original': len(restores)})
    write_json(output_dir / 'provenance.json', provenance)
    generate_reports(output_dir)
    return {'ok': True, 'approved': len(approves), 'restored': len(restores),
            'files_reprocessed': len(matched), 'decisions_file': archived.name,
            'reviewed_at': provenance['human_reviews'][-1]['reviewed_at'], 'reviewer': reviewer.strip()}


def main():
    parser = argparse.ArgumentParser(description='Apply validated local PII review decisions.')
    parser.add_argument('--decisions', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--reviewer', required=True)
    args = parser.parse_args()
    restores, approves = load_decisions(args.decisions)
    try:
        result = apply_review(args.output, restores + approves, args.reviewer)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Review saved: {result['approved']} kept redacted, {result['restored']} restored")


if __name__ == '__main__':
    main()
