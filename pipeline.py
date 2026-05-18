import hashlib
import json
import re
import shutil
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from reader import read_file
from classifier import classify
from cleaner import clean
from pii import strip_pii
from chunker import chunk
from writer import write_rag_chunks, write_finetune_record
from provenance import ProvenanceManifest
from reporter import generate_reports


SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg', '.txt'}

# Priority when multiple versions of the same document exist.
# Lower number = higher priority (will be processed; others skipped).
_EXT_PRIORITY = {'.txt': 0, '.pdf': 1, '.docx': 2, '.pptx': 3, '.eml': 4, '.msg': 5}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


_OCR_SUFFIXES = ('.ocr', '-ocr', '_ocr', '-ocr.pdf', ' searchable copy')


def _normalize_stem(path: Path) -> str:
    """Strip OCR suffixes to get the canonical document name for grouping."""
    stem = path.stem
    for suffix in _OCR_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[:len(stem) - len(suffix)]
    return stem.lower().strip()


def _is_ocr_version(path: Path) -> bool:
    """True if this file is an OCR-processed version of a scanned original."""
    stem_lower = path.stem.lower()
    return any(stem_lower.endswith(s) for s in _OCR_SUFFIXES)


def _select_best_versions(files: list, skip_log: list) -> list:
    """
    Given all candidate files, return one best version per unique document.

    Rules (applied in order):
    1. Exact SHA-256 duplicates → keep one (smallest path alphabetically), skip rest.
    2. Same document, multiple formats → prefer by _EXT_PRIORITY
       (.txt beats .ocr.pdf beats .pdf, etc.)
    """
    # Step 1: deduplicate by SHA-256
    seen_hashes = {}
    deduped = []
    for f in sorted(files):
        try:
            h = _sha256(f)
        except OSError:
            deduped.append(f)
            continue
        if h in seen_hashes:
            skip_log.append({
                'path': str(f),
                'reason': 'exact_duplicate',
                'duplicate_of': str(seen_hashes[h]),
            })
        else:
            seen_hashes[h] = f
            deduped.append(f)

    # Step 2: prefer best format per normalized stem
    groups = defaultdict(list)
    for f in deduped:
        groups[_normalize_stem(f)].append(f)

    selected = []
    for stem, group in groups.items():
        if len(group) == 1:
            selected.append(group[0])
            continue
        # Sort by extension priority, then path length (prefer shorter paths = root over subdir)
        best = sorted(group, key=lambda f: (
            _EXT_PRIORITY.get(f.suffix.lower(), 99),
            0 if _is_ocr_version(f) else 1,  # OCR PDF beats non-OCR PDF
            len(str(f)),
        ))[0]
        selected.append(best)
        for other in group:
            if other != best:
                skip_log.append({
                    'path': str(other),
                    'reason': 'superseded_by_better_version',
                    'superseded_by': str(best),
                })

    return selected


@dataclass
class ProcessResult:
    path: Path
    doc_type: str
    classification_confidence: float
    ocr: bool
    ocr_confidence: Optional[float]
    pii_stripped: bool
    faker_substitutions: int
    review_flags: int
    chunk_count: int
    token_count: int
    skipped: bool = False
    skip_reason: Optional[str] = None


def process_file(path: Path, output_dir: Path, dry_run: bool = False) -> ProcessResult:
    read_result = read_file(path)

    if read_result is None:
        if not dry_run:
            ocr_q = output_dir / 'review' / 'ocr_queue'
            ocr_q.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, ocr_q / path.name)
        return ProcessResult(
            path=path, doc_type='unknown', classification_confidence=0.0,
            ocr=True, ocr_confidence=None, pii_stripped=False,
            faker_substitutions=0, review_flags=0, chunk_count=0, token_count=0,
            skipped=True, skip_reason='ocr_confidence_low',
        )

    classify_result = classify(path, read_result.text)
    doc_type = classify_result.doc_type
    text = clean(read_result.text)

    pii_result = None
    if doc_type in ('private', 'uncertain'):
        pii_result = strip_pii(text, path.name, output_mode='finetune')
        text = pii_result.text

    faker_subs = pii_result.substitutions if pii_result else 0
    flag_count = len(pii_result.review_flags) if pii_result else 0
    chunks = chunk(text, doc_type)
    token_count = sum(c.token_count for c in chunks)

    if not dry_run:
        if pii_result and pii_result.review_flags:
            review_log = output_dir / 'review' / 'review_log.jsonl'
            review_log.parent.mkdir(parents=True, exist_ok=True)
            with open(review_log, 'a') as f:
                for flag in pii_result.review_flags:
                    f.write(json.dumps(flag) + '\n')

        if doc_type in ('caselaw', 'published'):
            extra = _caselaw_meta(text) if doc_type == 'caselaw' else {}
            extra['ocr'] = read_result.ocr
            write_rag_chunks(chunks, path.name, doc_type, extra, output_dir / 'rag')

        if doc_type in ('private', 'uncertain', 'published'):
            anon_id = f"anon_{abs(hash(path.name)):08d}"
            write_finetune_record(
                text=text,
                anon_id=anon_id,
                doc_type=doc_type,
                pii_stripped=doc_type in ('private', 'uncertain'),
                faker_substitutions=faker_subs,
                review_flags=flag_count,
                token_count=token_count,
                output_file=output_dir / 'finetune' / 'dataset.jsonl',
            )

    return ProcessResult(
        path=path,
        doc_type=doc_type,
        classification_confidence=classify_result.confidence,
        ocr=read_result.ocr,
        ocr_confidence=read_result.ocr_confidence,
        pii_stripped=doc_type in ('private', 'uncertain'),
        faker_substitutions=faker_subs,
        review_flags=flag_count,
        chunk_count=len(chunks),
        token_count=token_count,
    )


def _caselaw_meta(text: str) -> dict:
    meta = {}
    m = re.search(r'(\w[\w\s,\.]+v\.\s+[\w\s,\.]+,\s+\d+\s+\S+\s+\d+)', text[:2000])
    if m:
        meta['citation'] = m.group(1).strip()
    y = re.search(r'\((?:\w+\.?\s+)?(\d{4})\)', text[:2000])
    if y:
        meta['year'] = int(y.group(1))
    c = re.search(r'\((\d+(?:st|nd|rd|th) Cir\.|S\.Ct\.)[^)]*\)', text[:2000])
    if c:
        meta['court'] = c.group(1)
    return meta


def process_directory(
    input_dir: Path,
    output_dir: Path,
    dry_run: bool = False,
    workers: int = 1,
    sidecar_path: Optional[Path] = None,
) -> None:
    all_files = [
        p for p in input_dir.rglob('*')
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not all_files:
        print(f"No supported files in {input_dir}")
        return

    skip_log: list = []
    files = _select_best_versions(all_files, skip_log)

    print(f"Found {len(all_files)} supported file(s) → {len(files)} to process "
          f"({len(all_files) - len(files)} skipped as duplicates/superseded)."
          f"{' DRY RUN.' if dry_run else ''}")
    for entry in skip_log:
        reason = entry['reason']
        detail = entry.get('duplicate_of') or entry.get('superseded_by', '')
        print(f"  SKIP [{reason}] {Path(entry['path']).name}"
              f"{f' → use {Path(detail).name}' if detail else ''}")
    manifest = ProvenanceManifest(output_dir, sidecar_path,
                                  source_dir=input_dir) if not dry_run else None
    results = []

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(process_file, f, output_dir, dry_run): f for f in files}
            for fut in as_completed(futures):
                results.append(fut.result())
    else:
        for f in files:
            results.append(process_file(f, output_dir, dry_run))

    for r in results:
        _print_result(r, dry_run)

    if manifest:
        for r in results:
            manifest.add_file(
                original_path=r.path,
                doc_type=r.doc_type,
                classification_confidence=r.classification_confidence,
                ocr=r.ocr,
                ocr_confidence=r.ocr_confidence,
                pii_stripped=r.pii_stripped,
                faker_substitutions=r.faker_substitutions,
                review_flags=r.review_flags,
                chunk_count=r.chunk_count,
                token_count=r.token_count,
                skipped=r.skipped,
                skip_reason=r.skip_reason,
            )
        manifest.save()
        generate_reports(output_dir)
        print(f"\nReports written to {output_dir}/")
        print(f"  summary.html        — open in browser, print to PDF")
        print(f"  review/review_log.csv — PII flags for spreadsheet review")


def _print_result(r: ProcessResult, dry_run: bool) -> None:
    prefix = '[DRY RUN] ' if dry_run else ''
    status = 'SKIP' if r.skipped else r.doc_type.upper()
    flags = f" | {r.review_flags} PII flags" if r.review_flags else ''
    print(f"{prefix}{status:12s} {r.path.name} ({r.token_count} tokens{flags})")
