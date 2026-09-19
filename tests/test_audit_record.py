"""The two redaction records and their storage contract (U8).

R9  a durable evidence record: type, location, count, anonymous id, no values.
R18 a transient verification record: the values, deleted when the batch is
    accepted.
R10 nothing with a value in it goes inside the output directory.
"""
import json
import os
import stat
from pathlib import Path

import pytest

from pipeline import process_directory, process_file
from writer import (
    accept_batch,
    open_batch,
    records_root,
    unaccepted_batches,
    write_evidence_record,
)


@pytest.fixture(autouse=True)
def isolated_records(tmp_path, monkeypatch):
    """Never touch the real records location from a test."""
    monkeypatch.setenv('LEGAL_DOC_RECORDS_DIR', str(tmp_path / 'records'))
    return tmp_path / 'records'


def _evidence_records() -> list:
    directory = records_root() / 'evidence'
    return [json.loads(p.read_text()) for p in sorted(directory.glob('*.json'))]


def _verification_records(batch_id: str) -> list:
    directory = records_root() / 'verification' / batch_id
    return [json.loads(p.read_text())
            for p in sorted(directory.glob('*.json')) if p.name != 'batch.json']


def _current_batch() -> str:
    batches = sorted((records_root() / 'verification').glob('*/batch.json'))
    assert batches, "no batch was opened"
    return json.loads(batches[-1].read_text())['batch_id']


def _run(tmp_dir, discharge_text) -> Path:
    inp, out = tmp_dir / "input", tmp_dir / "output"
    inp.mkdir(parents=True)
    (inp / "discharge_summary.txt").write_text(discharge_text('copyright'))
    process_directory(inp, out, dry_run=False)
    return out


# --- what each record holds ---------------------------------------------------

def test_every_redaction_appears_in_the_evidence_record(tmp_dir, discharge_text):
    _run(tmp_dir, discharge_text)
    records = _evidence_records()
    assert len(records) == 1
    record = records[0]
    assert record['total_redacted'] > 0
    # High-confidence ones included: today's predecessor logged only the
    # medium-confidence band, so most redactions were recorded nowhere but an
    # aggregate count.
    assert 'US_SSN' in record['counts_by_type']
    for entry in record['identifiers']:
        assert {'entity_type', 'start', 'end', 'score', 'redacted'} <= set(entry)


def test_the_evidence_record_contains_no_original_value(tmp_dir, discharge_text,
                                                        discharge_ids):
    """Asserted against the fixture's known identifiers, not against the tool's
    own detections -- a check bound to the tool's findings would pass even if a
    recognizer silently stopped firing."""
    _run(tmp_dir, discharge_text)
    written = json.dumps(_evidence_records())
    for value in discharge_ids.values():
        assert value not in written, f"{value!r} is in the durable record"


def test_the_verification_record_holds_the_values(tmp_dir, discharge_text,
                                                  discharge_ids):
    """It exists so a human can check a batch was scrubbed correctly, so it is
    the one artifact that legitimately carries them."""
    _run(tmp_dir, discharge_text)
    written = json.dumps(_verification_records(_current_batch()))
    assert discharge_ids['ssn'] in written


def test_the_id_to_filename_mapping_lives_in_the_durable_record(tmp_dir,
                                                                discharge_text):
    """It has to survive batch acceptance: an evidence entry that cannot be
    tied back to a document stops answering the question the record exists to
    answer."""
    _run(tmp_dir, discharge_text)
    record = _evidence_records()[0]
    assert record['source']['filename'] == 'discharge_summary.txt'
    assert record['anon_id'].startswith('anon_')


# --- the storage contract -----------------------------------------------------

def test_records_live_outside_the_output_directory(tmp_dir, discharge_text):
    out = _run(tmp_dir, discharge_text)
    assert records_root() not in out.parents and records_root() != out
    assert not (out / 'review' / 'review_log.jsonl').exists()


@pytest.mark.parametrize('subdir', ['evidence', 'verification'])
def test_directories_are_0700_and_files_0600(tmp_dir, discharge_text, subdir):
    """Set explicitly at creation rather than inherited from the umask."""
    old_umask = os.umask(0o000)  # a permissive umask must not loosen them
    try:
        _run(tmp_dir, discharge_text)
    finally:
        os.umask(old_umask)

    root = records_root() / subdir
    assert stat.S_IMODE(root.stat().st_mode) == 0o700, f"{root} is not 0700"
    for record in root.rglob('*.json'):
        assert stat.S_IMODE(record.stat().st_mode) == 0o600, f"{record} is not 0600"


def test_an_interrupted_write_leaves_no_half_record(tmp_dir, monkeypatch):
    """Temp file then rename, so a crash cannot leave a truncated record that
    still parses as complete."""
    import writer

    original_replace = Path.replace

    def die_before_rename(self, target):
        raise KeyboardInterrupt("killed mid-write")

    monkeypatch.setattr(Path, 'replace', die_before_rename)
    with pytest.raises(KeyboardInterrupt):
        write_evidence_record('key-a', 'anon_a', 'private', [], 0.30)
    monkeypatch.setattr(Path, 'replace', original_replace)

    assert not (records_root() / 'evidence' / 'key-a.json').exists()


def test_records_are_keyed_so_two_same_named_files_do_not_collide(tmp_dir):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    for folder, body in (('client_a', 'hypertension'), ('client_b', 'ankle sprain')):
        (inp / folder).mkdir(parents=True)
        (inp / folder / "visit.txt").write_text(
            f"Office Visit Note\nPatient: Wanda Ferris\n"
            f"Medical Record Number: 4417392\nSeen for {body}.\n"
        )
    process_directory(inp, out, dry_run=False, workers=4)

    assert len(_evidence_records()) == 2
    assert len({r['anon_id'] for r in _evidence_records()}) == 2


# --- batch lifetime -----------------------------------------------------------

def test_accepting_a_batch_deletes_values_and_keeps_the_evidence(tmp_dir,
                                                                 discharge_text):
    _run(tmp_dir, discharge_text)
    batch_id = _current_batch()
    assert _verification_records(batch_id)

    accept_batch(batch_id)

    assert not (records_root() / 'verification' / batch_id).exists()
    records = _evidence_records()
    assert len(records) == 1 and records[0]['total_redacted'] > 0


def test_an_unaccepted_batch_survives_and_is_named_on_the_next_run(tmp_dir,
                                                                   discharge_text,
                                                                   capsys):
    out = _run(tmp_dir, discharge_text)
    first_batch = _current_batch()

    process_directory(tmp_dir / "input", out, dry_run=False)

    assert (records_root() / 'verification' / first_batch).exists(), (
        "an unaccepted batch was aged out without anyone accepting it"
    )
    assert first_batch in capsys.readouterr().out


def test_nothing_accepts_a_batch_implicitly(tmp_dir, discharge_text):
    _run(tmp_dir, discharge_text)
    batch_id = _current_batch()
    info = json.loads((records_root() / 'verification' / batch_id / 'batch.json').read_text())
    assert info['accepted'] is False
    assert unaccepted_batches() and unaccepted_batches()[0]['batch_id'] == batch_id


# --- what the report may show -------------------------------------------------

def test_the_report_still_lists_identifier_types_and_counts(tmp_dir, discharge_text):
    """Asserted against a fixture with known non-zero identifiers, so an
    empty-flag regression fails rather than passing quietly. Relocating the
    record without rewiring reporter.py would have made the summary report
    zero identifiers instead of failing."""
    out = _run(tmp_dir, discharge_text)
    html = (out / 'summary.html').read_text()
    assert 'US_SSN' in html
    csv_text = (out / 'review' / 'review_log.csv').read_text()
    assert 'US_SSN' in csv_text
    assert 'entity_type,count' in csv_text.splitlines()[0]


def test_the_report_carries_no_value_and_no_context_excerpt(tmp_dir, discharge_text,
                                                            discharge_ids):
    out = _run(tmp_dir, discharge_text)
    for name in ('summary.html', 'review/review_log.csv'):
        body = (out / name).read_text()
        for value in discharge_ids.values():
            assert value not in body, f"{value!r} is in {name}"


def test_review_flags_keeps_its_meaning_and_the_total_is_a_new_field(tmp_dir,
                                                                     discharge_text):
    """re_id_risk.py sorts by review_flags to choose which records get the paid
    adversarial review first. Redefining it as a total would silently invert
    that ordering with no visible error."""
    out = _run(tmp_dir, discharge_text)
    record = json.loads((out / 'finetune' / 'dataset.jsonl').read_text().strip())
    meta = record['metadata']

    evidence = _evidence_records()[0]
    ambiguous = sum(1 for e in evidence['identifiers']
                    if 0.30 <= e['score'] < 0.85)
    assert meta['review_flags'] == ambiguous
    assert meta['total_redactions'] == evidence['total_redacted']
    assert meta['total_redactions'] > meta['review_flags'], (
        "fixture should contain high-confidence redactions too, or this "
        "test cannot tell the two fields apart"
    )


def test_a_held_back_document_still_produces_a_record(tmp_dir):
    src = tmp_dir / "scanned.txt"
    src.write_text("Intake Form\nPatient: Wanda Ferris\nMedical Record Number: \n")
    result = process_file(src, tmp_dir / "output", dry_run=False)

    assert result.skipped is True
    records = _evidence_records()
    assert len(records) == 1
    assert records[0]['skip_reason'] == 'unresolved_identifier_label'
