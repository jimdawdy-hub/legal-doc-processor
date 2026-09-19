import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from chunker import Chunk
from utils import sha256_text

# --- the redaction records ---------------------------------------------------
#
# Two artifacts with different lifetimes, not one (KTD10).
#
#   evidence/      durable. Identifier type, location, count and the document's
#                  anonymous id, and no original value. This is what backs the
#                  client-facing claim, and it can be kept indefinitely because
#                  losing it discloses nothing (R9).
#
#   verification/  the original values, so a human can check that a batch was
#                  scrubbed correctly. Deleted when that batch is accepted
#                  (R18). Without that deletion the tool accumulates a
#                  cross-client cleartext index of every identifier it has ever
#                  seen -- permanent, growing with every run, and a single
#                  point whose compromise is a breach across the whole client
#                  base rather than one matter. File permissions do not
#                  mitigate that; a lifetime does.
#
# Both live outside the output directory, because the output directory is the
# one thing that leaves the machine (KTD7, R10).

RECORDS_DIR_ENV = 'LEGAL_DOC_RECORDS_DIR'
_DEFAULT_RECORDS_DIR = Path.home() / '.local' / 'share' / 'legal-doc-processor'


def records_root() -> Path:
    """Where the redaction records live -- never inside the output directory.

    A single stable location, overridable so it can be placed where backup
    tooling includes or excludes it deliberately rather than by accident.
    """
    return Path(os.environ.get(RECORDS_DIR_ENV) or _DEFAULT_RECORDS_DIR)


def _secure_dir(path: Path) -> Path:
    """Create a directory 0700, set explicitly rather than left to the umask.

    Every level down from the records root, not just the leaf: mkdir(parents=True)
    creates intermediates with the process umask, which left the directory
    holding every verification record world-readable under a permissive one.
    """
    root = records_root()
    path.mkdir(parents=True, exist_ok=True)
    current = path
    while True:
        current.chmod(0o700)
        if current == root or root not in current.parents:
            break
        current = current.parent
    return path


def _write_secure_json(path: Path, payload: dict) -> None:
    """Write 0600, atomically.

    Temp file then rename, so an interrupted run cannot leave a truncated
    record that still parses as complete, and cannot leave one document's
    record under another's key.
    """
    _secure_dir(path.parent)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def write_evidence_record(
    doc_key: str,
    anon_id: str,
    doc_type: str,
    detections: List[dict],
    redaction_floor: float,
    source_filename: Optional[str] = None,
    source_dir: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> dict:
    """The durable record behind the client-facing claim (R9).

    Carries type, location, count and the anonymous id, and no original value.

    It also carries the id-to-filename mapping, which has to live in *this*
    record rather than the deletable one: an evidence entry that cannot be tied
    back to a document stops answering the question the record exists to
    answer. A filename is source-identifying but it is not an identifier value,
    and this record is outside the output directory, so R10 and R16 are
    unaffected.
    """
    entries = [
        {
            'entity_type': d['entity_type'],
            'start': d['start'],
            'end': d['end'],
            'score': d['score'],
            'redacted': d['score'] >= redaction_floor,
        }
        for d in detections
    ]
    counts: dict = {}
    for entry in entries:
        counts[entry['entity_type']] = counts.get(entry['entity_type'], 0) + 1

    record = {
        'anon_id': anon_id,
        'doc_key': doc_key,
        'doc_type': doc_type,
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'skip_reason': skip_reason,
        'counts_by_type': counts,
        'total_detected': len(entries),
        'total_redacted': sum(1 for e in entries if e['redacted']),
        'identifiers': entries,
        'source': {'filename': source_filename, 'directory': source_dir},
    }
    _write_secure_json(records_root() / 'evidence' / f'{doc_key}.json', record)
    return record


def write_verification_record(
    doc_key: str,
    anon_id: str,
    batch_id: str,
    detections: List[dict],
    redaction_floor: float,
) -> Path:
    """The transient record a human checks a batch against (R18).

    Holds the original values. Deleted when the batch is accepted; nothing
    accepts a batch implicitly.
    """
    record = {
        'anon_id': anon_id,
        'doc_key': doc_key,
        'batch_id': batch_id,
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'identifiers': [
            {
                'entity_type': d['entity_type'],
                'start': d['start'],
                'end': d['end'],
                'score': d['score'],
                'original_value': d.get('value'),
                'redacted': d['score'] >= redaction_floor,
            }
            for d in detections
        ],
    }
    path = records_root() / 'verification' / batch_id / f'{doc_key}.json'
    _write_secure_json(path, record)
    return path


def open_batch(batch_id: str, output_dir: Path) -> Path:
    path = records_root() / 'verification' / batch_id / 'batch.json'
    _write_secure_json(path, {
        'batch_id': batch_id,
        'opened_at': datetime.now(timezone.utc).isoformat(),
        'output_dir': str(output_dir),
        'accepted': False,
    })
    return path


def unaccepted_batches(exclude: Optional[str] = None) -> List[dict]:
    """Batches whose verification records are still on disk.

    Reported on the next run rather than aged out on a timer: a batch left
    unaccepted is exactly how a transient cleartext record quietly becomes a
    permanent one.
    """
    root = records_root() / 'verification'
    if not root.exists():
        return []
    open_batches = []
    for batch_file in sorted(root.glob('*/batch.json')):
        try:
            info = json.loads(batch_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if info.get('accepted') or info.get('batch_id') == exclude:
            continue
        info['record_count'] = len(list(batch_file.parent.glob('*.json'))) - 1
        open_batches.append(info)
    return open_batches


def accept_batch(batch_id: str) -> int:
    """Accept a batch and delete its verification records (R18).

    The evidence records are untouched -- they carry no values and are what the
    client-facing claim rests on.
    """
    batch_dir = records_root() / 'verification' / batch_id
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"no open batch {batch_id!r}")
    removed = 0
    for record in sorted(batch_dir.glob('*.json')):
        record.unlink()
        removed += 1
    batch_dir.rmdir()
    return removed


def _slugify(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r'[^a-z0-9]+', '_', stem.lower()).strip('_')


def write_rag_chunks(
    chunks: List[Chunk],
    source_filename: str,
    doc_type: str,
    extra_metadata: dict,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(source_filename)
    out_path = output_dir / f"{slug}.jsonl"

    with open(out_path, 'w') as f:
        for c in chunks:
            record = {
                'id': f"{slug}_chunk_{c.index:03d}",
                'text': c.text,
                'metadata': {
                    'source': source_filename,
                    'doc_type': doc_type,
                    'chunk_index': c.index,
                    'total_chunks': c.total,
                    'token_count': c.token_count,
                    **extra_metadata,
                },
            }
            f.write(json.dumps(record) + '\n')


def write_finetune_record(
    text: str,
    anon_id: str,
    doc_type: str,
    pii_stripped: bool,
    faker_substitutions: int,
    review_flags: int,
    token_count: int,
    output_file: Path,
    lock: Optional[object] = None,
    doc_key: Optional[str] = None,
    content_sha256: Optional[str] = None,
    total_redactions: int = 0,
) -> None:
    """Insert or replace this document's record (R14).

    Records used to be appended, so every re-run added a byte-identical
    duplicate of every document already processed. They are now keyed by
    doc_key and replaced in place.

    Deliberately still one shared dataset.jsonl rather than a file per
    document on the write_rag_chunks model: second_pass.py hardcodes this
    single path to find finetune records, and splitting the file would break
    it silently.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'text': text,
        'metadata': {
            'source': anon_id,
            'doc_type': doc_type,
            'pii_stripped': pii_stripped,
            'faker_substitutions': faker_substitutions,
            'review_flags': review_flags,
            'total_redactions': total_redactions,
            'token_count': token_count,
            'doc_key': doc_key or anon_id,
            'content_sha256': content_sha256,
            # What we wrote. If the stored text stops matching this, something
            # downstream has edited the record and a re-run must not clobber it.
            'text_sha256': sha256_text(text),
        },
    }
    if lock:
        # The lock has to span the whole read-merge-rewrite cycle, not just the
        # write: it previously guarded an individual append, which is not
        # enough once the write becomes read-then-rewrite.
        with lock:
            _upsert(output_file, record)
    else:
        _upsert(output_file, record)


def _was_edited_downstream(record: dict) -> bool:
    """True if this record's text is no longer what the pipeline wrote.

    second_pass.py patches records in place. A re-run over an unchanged source
    must not quietly undo that hardening.
    """
    stored = record.get('metadata', {}).get('text_sha256')
    return stored is not None and stored != sha256_text(record.get('text', ''))


def _upsert(output_file: Path, record: dict) -> None:
    key = record['metadata']['doc_key']
    order: List[str] = []
    existing: dict = {}
    if output_file.exists():
        for line in output_file.read_text().splitlines():
            if not line.strip():
                continue
            prior = json.loads(line)
            meta = prior.get('metadata', {})
            prior_key = meta.get('doc_key') or meta.get('source')
            if prior_key not in existing:
                order.append(prior_key)
            existing[prior_key] = prior

    held = existing.get(key)
    if (held is not None
            and _was_edited_downstream(held)
            and held['metadata'].get('content_sha256') == record['metadata']['content_sha256']):
        return  # already hardened, and the source has not changed since

    if key not in existing:
        order.append(key)
    existing[key] = record

    # Written to a temporary file and renamed, so an interrupted run cannot
    # leave a truncated dataset that still parses as complete.
    tmp = output_file.with_name(output_file.name + '.tmp')
    tmp.write_text(''.join(json.dumps(existing[k]) + '\n' for k in order))
    tmp.replace(output_file)
