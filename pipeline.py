import re
import shutil
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing import Lock
from pathlib import Path
from typing import Optional

from utils import anon_id, document_key, sha256_file, SUPPORTED_EXTENSIONS
from reader import read_file
from classifier import classify
from cleaner import clean
from pii import strip_pii, HIGH_CONFIDENCE, LOW_CONFIDENCE, MISCLASSIFICATION_SIGNALS
from chunker import chunk
from writer import (
    open_batch,
    records_root,
    unaccepted_batches,
    write_evidence_record,
    write_finetune_record,
    write_rag_chunks,
    write_verification_record,
)
from provenance import ProvenanceManifest
from reporter import generate_reports

# Priority when multiple versions of the same document exist.
# Lower number = higher priority (will be processed; others skipped).
_EXT_PRIORITY = {'.txt': 0, '.pdf': 1, '.docx': 2, '.pptx': 3, '.eml': 4, '.msg': 5}


_write_lock: Optional[Lock] = None


def _init_write_lock(lock: Lock) -> None:
    global _write_lock
    _write_lock = lock


def _get_write_lock() -> Optional[Lock]:
    return _write_lock


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
            h = sha256_file(f)
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

    # Step 2: prefer best format per normalized stem, within one directory.
    #
    # Grouping on the stem alone reached across folders: two different
    # clients' 'visit.txt' landed in one group and the second was dropped from
    # processing entirely, logged as 'superseded_by_better_version'. This step
    # exists to choose between formats of the same document -- opinion.pdf vs
    # opinion.txt vs opinion-ocr.pdf -- which are siblings in one directory.
    # Genuinely identical files in different folders are still collapsed by
    # the SHA-256 pass above, which is the check that can prove they are the
    # same document.
    groups = defaultdict(list)
    for f in deduped:
        groups[(f.parent, _normalize_stem(f))].append(f)

    selected = []
    for _, group in groups.items():
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


def _hold_back(path: Path, output_dir: Path, queue: str) -> None:
    """Copy a document aside instead of emitting it (KTD11).

    A held-back document cannot reach a vendor by accident; a flag on an
    emitted one relies on someone reading it.
    """
    destination = output_dir / 'review' / queue
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination / path.name)


def _quarantine_reason(doc_type: str, pii_result) -> Optional[str]:
    """Whether this document must be held back, and why.

    This adjudication lives here because pipeline.py is the only place holding
    both the detection facts and the classification. pii.py never receives
    doc_type (KTD5).
    """
    if pii_result.unresolved_labels:
        # Plan Q1: a recognised label with no resolvable value after it. A
        # structural failure, not a low-confidence guess -- the document says
        # it carries an identifier and we could not read it.
        return 'unresolved_identifier_label'

    if doc_type in ('caselaw', 'published'):
        # KTD13: a confident healthcare identifier in a document classified
        # public is evidence the classifier was wrong. Ambient types -- person,
        # date, location -- never trigger this, or every real opinion would be
        # held back.
        for detection in pii_result.detections:
            if (detection['entity_type'] in MISCLASSIFICATION_SIGNALS
                    and detection['score'] >= HIGH_CONFIDENCE):
                return 'pii_in_public_document'

    return None


def _new_batch_id() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]


def _record_redactions(path: Path, doc_type: str, pii_result, batch_id: str,
                       output_dir: Path, skip_reason: Optional[str] = None) -> dict:
    """Write both redaction records, outside the output directory (U8).

    Per-document files following write_rag_chunks' shape, so no lock is needed
    under --workers.
    """
    key = document_key(path)
    label = anon_id(path)
    evidence = write_evidence_record(
        doc_key=key, anon_id=label, doc_type=doc_type,
        detections=pii_result.detections, redaction_floor=LOW_CONFIDENCE,
        source_filename=path.name, source_dir=str(path.parent.resolve()),
        skip_reason=skip_reason,
    )
    write_verification_record(
        doc_key=key, anon_id=label, batch_id=batch_id,
        detections=pii_result.detections, redaction_floor=LOW_CONFIDENCE,
    )
    return evidence


def process_file(path: Path, output_dir: Path, dry_run: bool = False,
                 batch_id: Optional[str] = None) -> ProcessResult:
    batch_id = batch_id or _new_batch_id()
    read_result = read_file(path)

    if read_result is None:
        if not dry_run:
            _hold_back(path, output_dir, 'ocr_queue')
        return ProcessResult(
            path=path, doc_type='unknown', classification_confidence=0.0,
            ocr=True, ocr_confidence=None, pii_stripped=False,
            faker_substitutions=0, review_flags=0, chunk_count=0, token_count=0,
            skipped=True, skip_reason='ocr_confidence_low',
        )

    classify_result = classify(path, read_result.text)
    doc_type = classify_result.doc_type
    text = clean(read_result.text)

    # KTD5: detection runs on every document, whatever it was classified as.
    # Classification decides how a document is treated; it no longer decides
    # whether identifiers are looked for at all. Replacement stays limited to
    # private/uncertain here -- U7 adjudicates what a detection in a public
    # document means (quarantine), which is why pii.py never sees doc_type.
    pii_result = strip_pii(text, path.name, output_mode='finetune')
    redacted = doc_type in ('private', 'uncertain')
    if redacted:
        text = pii_result.text

    hold_back = _quarantine_reason(doc_type, pii_result)
    if hold_back:
        if not dry_run:
            _hold_back(path, output_dir, 'pii_queue')
            # A held-back document still gets a record of what was found in it.
            # Its reason for being held back is exactly what someone will want
            # to look up later.
            _record_redactions(path, doc_type, pii_result, batch_id, output_dir,
                               skip_reason=hold_back)
        return ProcessResult(
            path=path, doc_type=doc_type,
            classification_confidence=classify_result.confidence,
            ocr=read_result.ocr, ocr_confidence=read_result.ocr_confidence,
            pii_stripped=False, faker_substitutions=0, review_flags=0,
            chunk_count=0, token_count=0,
            skipped=True, skip_reason=hold_back,
        )

    faker_subs = pii_result.substitutions if redacted else 0
    flag_count = len(pii_result.review_flags) if redacted else 0
    chunks = chunk(text, doc_type)
    token_count = sum(c.token_count for c in chunks)

    evidence = None
    if not dry_run:
        evidence = _record_redactions(path, doc_type, pii_result, batch_id, output_dir)

        if doc_type in ('caselaw', 'published'):
            extra = _caselaw_meta(text) if doc_type == 'caselaw' else {}
            extra['ocr'] = read_result.ocr
            write_rag_chunks(chunks, path.name, doc_type, extra, output_dir / 'rag')

        if doc_type in ('private', 'uncertain', 'published'):
            write_finetune_record(
                text=text,
                anon_id=anon_id(path),
                doc_key=document_key(path),
                content_sha256=sha256_file(path),
                doc_type=doc_type,
                pii_stripped=doc_type in ('private', 'uncertain'),
                faker_substitutions=faker_subs,
                # Unchanged in name and meaning: the count of *ambiguous*
                # detections. re_id_risk.py sorts by it to choose which
                # records get the paid adversarial review first, so
                # redefining it as a total would silently invert that
                # ordering with no visible error. The total goes beside it.
                review_flags=flag_count,
                total_redactions=evidence['total_redacted'] if redacted else 0,
                token_count=token_count,
                output_file=output_dir / 'finetune' / 'dataset.jsonl',
                lock=_get_write_lock(),
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


def process_file_safely(path: Path, output_dir: Path, dry_run: bool = False,
                        batch_id: Optional[str] = None) -> ProcessResult:
    """process_file, but one bad document costs only that document.

    An unhandled exception used to propagate out of the loop, so the manifest
    and the report were never written at all: a long unattended run could fail
    wholesale and leave no record saying so. Module level rather than a nested
    closure because ProcessPoolExecutor has to pickle it.
    """
    try:
        return process_file(path, output_dir, dry_run, batch_id)
    except Exception as exc:  # noqa: BLE001 - the whole point is to catch everything
        print(f"  ERROR  {path.name}: {type(exc).__name__}: {exc}")
        return ProcessResult(
            path=path, doc_type='unknown', classification_confidence=0.0,
            ocr=False, ocr_confidence=None, pii_stripped=False,
            faker_substitutions=0, review_flags=0, chunk_count=0, token_count=0,
            skipped=True, skip_reason='processing_error',
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
    batch_id = _new_batch_id()
    if not dry_run:
        open_batch(batch_id, output_dir)
    results = []

    if workers > 1:
        lock = Lock()
        with ProcessPoolExecutor(max_workers=workers,
                                  initializer=_init_write_lock,
                                  initargs=(lock,)) as ex:
            futures = {ex.submit(process_file_safely, f, output_dir, dry_run,
                                 batch_id): f for f in files}
            for fut in as_completed(futures):
                results.append(fut.result())
    else:
        for f in files:
            results.append(process_file_safely(f, output_dir, dry_run, batch_id))

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
        print(f"  review/review_log.csv — identifier types and counts")
        print(f"\nRedaction records (outside the output directory):")
        print(f"  {records_root()}/evidence/     — types, counts, locations; kept")
        print(f"  {records_root()}/verification/{batch_id}/ — original values")
        print(f"  Accept this batch to delete its original values:")
        print(f"    python3.12 review_pii.py --accept {batch_id}")

        stale = unaccepted_batches(exclude=batch_id)
        if stale:
            print(f"\n  {len(stale)} earlier batch(es) still hold original "
                  f"identifier values and have not been accepted:")
            for info in stale:
                print(f"    {info['batch_id']}  "
                      f"({info.get('record_count', 0)} documents, "
                      f"opened {info.get('opened_at', 'unknown')})")


def _print_result(r: ProcessResult, dry_run: bool) -> None:
    prefix = '[DRY RUN] ' if dry_run else ''
    status = 'SKIP' if r.skipped else r.doc_type.upper()
    flags = f" | {r.review_flags} PII flags" if r.review_flags else ''
    print(f"{prefix}{status:12s} {r.path.name} ({r.token_count} tokens{flags})")
