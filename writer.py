import json
import re
from pathlib import Path
from typing import List, Optional

from chunker import Chunk
from utils import sha256_text


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
