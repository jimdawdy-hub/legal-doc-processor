import json
import re
from pathlib import Path
from typing import List, Optional

from chunker import Chunk


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
) -> None:
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
        },
    }
    line = json.dumps(record) + '\n'
    if lock:
        with lock:
            with open(output_file, 'a') as f:
                f.write(line)
    else:
        with open(output_file, 'a') as f:
            f.write(line)
