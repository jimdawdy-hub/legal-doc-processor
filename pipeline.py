import json
import re
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import Lock
from pathlib import Path
from typing import Optional

from utils import sha256_file, SUPPORTED_EXTENSIONS, document_id, private_append
from reader import read_file
from classifier import classify
from cleaner import clean
from pii import strip_pii
from chunker import chunk
from writer import write_rag_chunks, write_finetune_record
from provenance import ProvenanceManifest
from reporter import generate_reports

_write_lock: Optional[Lock] = None


def _init_write_lock(lock: Lock) -> None:
    global _write_lock
    _write_lock = lock


def _get_write_lock() -> Optional[Lock]:
    return _write_lock


def _select_best_versions(files: list, skip_log: list) -> list:
    """Deduplicate identical bytes only; filenames do not establish equivalence."""
    seen_hashes = {}
    selected = []
    for path in sorted(files):
        try:
            digest = sha256_file(path)
        except OSError:
            selected.append(path)
            continue
        if digest in seen_hashes:
            skip_log.append({'path': str(path), 'reason': 'exact_duplicate',
                             'duplicate_of': str(seen_hashes[digest])})
        else:
            seen_hashes[digest] = path
            selected.append(path)
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


def process_file(path: Path, output_dir: Path, dry_run: bool = False,
                 preserve_public_text: bool = False, replacement: str = 'tokens') -> ProcessResult:
    read_result = read_file(path)

    if read_result is None:
        if not dry_run:
            ocr_q = output_dir / 'review' / 'ocr_queue'
            ocr_q.mkdir(parents=True, exist_ok=True, mode=0o700)
            queued = ocr_q / (document_id(path) + path.suffix.lower())
            shutil.copy2(path, queued)
            queued.chmod(0o600)
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
    redact = not (preserve_public_text and doc_type in ('caselaw', 'published'))
    if redact and not dry_run:
        pii_result = strip_pii(text, document_id(path),
                               output_mode='finetune' if replacement == 'synthetic' else 'rag')
        text = pii_result.text

    faker_subs = pii_result.substitutions if pii_result else 0
    flag_count = len(pii_result.review_flags) if pii_result else 0
    chunks = chunk(text, doc_type)
    token_count = sum(c.token_count for c in chunks)

    if not dry_run:
        if pii_result and pii_result.review_flags:
            review_log = output_dir / 'review' / 'review_log.jsonl'
            review_log.parent.mkdir(parents=True, exist_ok=True)
            lock = _get_write_lock()
            if lock:
                with lock:
                    with private_append(review_log) as f:
                        for flag in pii_result.review_flags:
                            f.write(json.dumps(flag) + '\n')
            else:
                with private_append(review_log) as f:
                    for flag in pii_result.review_flags:
                        f.write(json.dumps(flag) + '\n')

        if doc_type in ('caselaw', 'published'):
            extra = _caselaw_meta(text) if doc_type == 'caselaw' and not redact else {}
            extra['ocr'] = read_result.ocr
            extra['pii_stripped'] = redact
            write_rag_chunks(chunks, document_id(path), doc_type, extra, output_dir / 'rag')

        if doc_type in ('private', 'uncertain', 'published', 'caselaw'):
            anon_id = document_id(path)
            for part in chunks:
                write_finetune_record(
                    text=part.text, anon_id=anon_id, doc_type=doc_type,
                    pii_stripped=redact and not dry_run, faker_substitutions=faker_subs,
                    review_flags=flag_count, token_count=part.token_count,
                    output_file=output_dir / 'finetune' / 'dataset.jsonl',
                    lock=_get_write_lock(), chunk_index=part.index,
                    total_chunks=part.total, replacement=replacement,
                )

    return ProcessResult(
        path=path,
        doc_type=doc_type,
        classification_confidence=classify_result.confidence,
        ocr=read_result.ocr,
        ocr_confidence=read_result.ocr_confidence,
        pii_stripped=redact and not dry_run,
        faker_substitutions=faker_subs,
        review_flags=flag_count,
        chunk_count=len(chunks),
        token_count=token_count,
    )


def _caselaw_meta(text: str) -> dict:
    """Extract citation metadata from the first 2000 chars of caselaw text.

    Limitations: only matches standard federal reporter formats (e.g. "123 F.3d 456").
    State court opinions, unpublished orders, and non-standard citation formats
    will not be captured — those fields simply won't appear in the output metadata.
    """
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
    preserve_public_text: bool = False,
    replacement: str = 'tokens',
) -> None:
    input_dir, output_dir = input_dir.resolve(), output_dir.resolve()
    if not input_dir.is_dir():
        raise ValueError('Input must be an existing directory')
    if workers < 1:
        raise ValueError('workers must be at least 1')
    if replacement not in {'tokens', 'synthetic'}:
        raise ValueError('replacement must be tokens or synthetic')
    if input_dir == output_dir or input_dir in output_dir.parents or output_dir in input_dir.parents:
        raise ValueError('Input and output directories must not overlap')
    if not dry_run:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(output_dir.iterdir()):
            raise ValueError('Output directory must be empty; use a new directory for each batch')
        output_dir.chmod(0o700)
    all_files = [
        p for p in input_dir.rglob('*')
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUPPORTED_EXTENSIONS
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
        lock = Lock()
        with ProcessPoolExecutor(max_workers=workers,
                                  initializer=_init_write_lock,
                                  initargs=(lock,)) as ex:
            futures = {ex.submit(_process_safely, f, output_dir, dry_run, preserve_public_text, replacement): f for f in files}
            for fut in as_completed(futures):
                results.append(fut.result())
    else:
        for f in files:
            results.append(_process_safely(f, output_dir, dry_run, preserve_public_text, replacement))

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
                replacement=replacement,
            )
        manifest.save()
        generate_reports(output_dir)
        print(f"\nReports written to {output_dir}/")
        print("  summary.html        — open in browser, print to PDF")
        print("  review/review_log.csv — confidential PII flags for local review")
        errors = sum(r.skip_reason.startswith('processing_error:') for r in results if r.skip_reason)
        if errors:
            raise RuntimeError(f'{errors} file(s) failed; inspect provenance.json before using any output')


def _process_safely(path, output_dir, dry_run, preserve_public_text, replacement):
    try:
        return process_file(path, output_dir, dry_run, preserve_public_text, replacement)
    except Exception as exc:
        return ProcessResult(path, 'unknown', 0.0, False, None, False, 0, 0, 0, 0,
                             skipped=True, skip_reason='processing_error:' + type(exc).__name__)


def _print_result(r: ProcessResult, dry_run: bool) -> None:
    prefix = '[DRY RUN] ' if dry_run else ''
    status = 'SKIP' if r.skipped else r.doc_type.upper()
    flags = f" | {r.review_flags} PII flags" if r.review_flags else ''
    print(f"{prefix}{status:12s} {r.path.name} ({r.token_count} tokens{flags})")
