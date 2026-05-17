import json
import re
import shutil
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


SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg'}


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
    files = [
        p for p in input_dir.rglob('*')
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not files:
        print(f"No supported files in {input_dir}")
        return

    print(f"Found {len(files)} file(s).{' DRY RUN.' if dry_run else ''}")
    manifest = ProvenanceManifest(output_dir, sidecar_path) if not dry_run else None
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


def _print_result(r: ProcessResult, dry_run: bool) -> None:
    prefix = '[DRY RUN] ' if dry_run else ''
    status = 'SKIP' if r.skipped else r.doc_type.upper()
    flags = f" | {r.review_flags} PII flags" if r.review_flags else ''
    print(f"{prefix}{status:12s} {r.path.name} ({r.token_count} tokens{flags})")
