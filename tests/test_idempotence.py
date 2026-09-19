"""Re-running over an existing output directory replaces, never duplicates (U6).

The quarantine loop U7 adds is 'fix the document and run it again', so this
defect would be exercised on every manual test of everything after it.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import process_directory


def _write_doc(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "RIVERTON FAMILY MEDICINE\nOffice Visit Note\n\n"
        f"Patient: Wanda Ferris\nMedical Record Number: 4417392\n\n{body}\n"
    )
    return path


def _records(out_dir: Path) -> list:
    dataset = out_dir / "finetune" / "dataset.jsonl"
    if not dataset.exists():
        return []
    return [json.loads(l) for l in dataset.read_text().splitlines() if l.strip()]


def _provenance(out_dir: Path) -> dict:
    return json.loads((out_dir / "provenance.json").read_text())


def test_processing_the_same_directory_twice_yields_one_record_each(tmp_dir):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    _write_doc(inp / "visit_a.txt", "Seen for follow-up of hypertension.")
    _write_doc(inp / "visit_b.txt", "Seen for a routine physical.")

    process_directory(inp, out, dry_run=False)
    after_one = _records(out)
    process_directory(inp, out, dry_run=False)
    after_two = _records(out)

    assert len(after_one) == 2
    assert len(after_two) == 2, (
        f"a second run appended duplicates: {len(after_two)} records"
    )
    assert _provenance(out)['summary']['total_files'] == 2


def test_changed_document_has_its_record_replaced_not_duplicated(tmp_dir):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    doc = _write_doc(inp / "visit.txt", "Seen for follow-up of hypertension.")
    process_directory(inp, out, dry_run=False)

    _write_doc(doc, "Seen for a productive cough and fever.")
    process_directory(inp, out, dry_run=False)

    records = _records(out)
    assert len(records) == 1, f"expected one record, got {len(records)}"
    assert "cough" in records[0]['text']


def test_same_document_gets_the_same_id_in_separate_invocations(tmp_dir):
    """Python's hash() is salted per process: the previous anon_id derivation
    produced a different value on every invocation, and was not the eight
    digits its format string claimed. Keying replace-on-rerun on it would have
    failed silently and duplicated instead."""
    target = str(tmp_dir / "client_folder" / "visit.txt")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from utils import anon_id\n"
        "print(anon_id(Path(%r)))\n" % (str(Path.cwd()), target)
    )
    seen = {
        subprocess.run([sys.executable, '-c', script], capture_output=True,
                       text=True, check=True).stdout.strip()
        for _ in range(3)
    }
    assert len(seen) == 1, f"id changed between invocations: {seen}"


def test_same_filename_in_different_folders_gets_distinct_ids(tmp_dir):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    _write_doc(inp / "client_a" / "visit.txt", "Seen for hypertension.")
    _write_doc(inp / "client_b" / "visit.txt", "Seen for a sprained ankle.")

    process_directory(inp, out, dry_run=False)

    records = _records(out)
    assert len(records) == 2
    ids = {r['metadata']['source'] for r in records}
    assert len(ids) == 2, f"two client folders collided on one id: {ids}"
    keys = {f['doc_key'] for f in _provenance(out)['files']}
    assert len(keys) == 2, f"provenance collided on one key: {keys}"


def test_provenance_retains_entries_from_an_earlier_run(tmp_dir):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    first = _write_doc(inp / "visit_a.txt", "Seen for hypertension.")
    _write_doc(inp / "visit_b.txt", "Seen for a routine physical.")
    process_directory(inp, out, dry_run=False)

    first.unlink()
    process_directory(inp, out, dry_run=False)

    filenames = {f['original_filename'] for f in _provenance(out)['files']}
    assert 'visit_a.txt' in filenames, (
        "a document processed in the first run vanished from provenance"
    )
    assert _provenance(out)['summary']['total_files'] == 2


def test_a_record_hardened_by_second_pass_is_not_reverted(tmp_dir):
    """second_pass.py patches records in place. A re-run over an unchanged
    source must not quietly undo that hardening."""
    inp, out = tmp_dir / "input", tmp_dir / "output"
    _write_doc(inp / "visit.txt", "Seen for follow-up of hypertension.")
    process_directory(inp, out, dry_run=False)

    dataset = out / "finetune" / "dataset.jsonl"
    record = json.loads(dataset.read_text().strip())
    record['text'] = record['text'].replace("hypertension", "[CONDITION]")
    dataset.write_text(json.dumps(record) + '\n')

    process_directory(inp, out, dry_run=False)

    after = _records(out)
    assert len(after) == 1
    assert "[CONDITION]" in after[0]['text'], (
        "the re-run reverted a record that had already been hardened"
    )


def test_concurrent_workers_produce_one_record_per_document(tmp_dir):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    for i in range(8):
        _write_doc(inp / f"visit_{i}.txt", f"Seen in clinic for reason {i}.")

    process_directory(inp, out, dry_run=False, workers=4)

    records = _records(out)
    assert len(records) == 8, f"expected 8 records, got {len(records)}"
    assert len({r['metadata']['source'] for r in records}) == 8
