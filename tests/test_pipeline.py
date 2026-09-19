import json
import pytest
from pathlib import Path
from pipeline import process_file, process_directory, SUPPORTED_EXTENSIONS

def _write_eml(path: Path, body: str = "Client matter update.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        f"From: a@b.com\r\nTo: c@d.com\r\nSubject: Case\r\n"
        f"Content-Type: text/plain\r\n\r\n{body}".encode()
    )
    return path

def test_process_file_returns_process_result(tmp_dir):
    from pipeline import ProcessResult
    eml = _write_eml(tmp_dir / "test.eml")
    result = process_file(eml, tmp_dir / "output", dry_run=True)
    assert isinstance(result, ProcessResult)

def test_process_file_dry_run_writes_nothing(tmp_dir):
    eml = _write_eml(tmp_dir / "test.eml")
    out_dir = tmp_dir / "output"
    process_file(eml, out_dir, dry_run=True)
    assert not out_dir.exists()

def test_process_file_private_eml_strips_pii(tmp_dir):
    eml = _write_eml(tmp_dir / "test.eml", body="Client John Smith, SSN 123-45-6789.")
    out_dir = tmp_dir / "output"
    result = process_file(eml, out_dir, dry_run=False)
    assert result.pii_stripped is True
    finetune = out_dir / "finetune" / "dataset.jsonl"
    assert finetune.exists()
    record = json.loads(finetune.read_text())
    assert "123-45-6789" not in record["text"]

def test_process_directory_creates_provenance(tmp_dir):
    inp = tmp_dir / "input"
    inp.mkdir(parents=True, exist_ok=True)
    _write_eml(inp / "email1.eml")
    out_dir = tmp_dir / "output"
    process_directory(inp, out_dir, dry_run=False)
    assert (out_dir / "provenance.json").exists()

def test_process_directory_dry_run_no_output(tmp_dir):
    inp = tmp_dir / "input"
    inp.mkdir()
    _write_eml(inp / "email1.eml")
    out_dir = tmp_dir / "output"
    process_directory(inp, out_dir, dry_run=True)
    assert not out_dir.exists()

def test_supported_extensions_set():
    assert '.pdf' in SUPPORTED_EXTENSIONS
    assert '.eml' in SUPPORTED_EXTENSIONS
    assert '.msg' in SUPPORTED_EXTENSIONS
    assert '.docx' in SUPPORTED_EXTENSIONS
    assert '.pptx' in SUPPORTED_EXTENSIONS


# --- U2: detection runs on every document, whatever it was classified as -----

def _read_all_output_text(out_dir: Path) -> str:
    """Every byte the run wrote under output/, concatenated."""
    if not out_dir.exists():
        return ''
    return '\n'.join(
        p.read_text(errors='replace')
        for p in out_dir.rglob('*') if p.is_file()
    )


def _read_deliverable_text(out_dir: Path) -> str:
    """The files that are actually handed onward: RAG chunks and finetune
    records. The review record under output/review/ is audit material whose
    relocation out of this directory is R10's job, owned by U8."""
    return '\n'.join(
        p.read_text(errors='replace')
        for sub in ('rag', 'finetune')
        for p in (out_dir / sub).rglob('*') if p.is_file()
    ) if out_dir.exists() else ''


@pytest.mark.parametrize('footer', ['copyright', 'bar_association', 'issn'])
def test_medical_record_with_published_lookalike_is_scrubbed(
    tmp_dir, discharge_text, discharge_ids, footer
):
    """The headline defect: one copyright / Bar Association / ISSN line used to
    classify a discharge summary as 'published', and published skipped the
    scrubber, so the record was written out with the SSN intact."""
    src = tmp_dir / "discharge_summary.txt"
    src.write_text(discharge_text(footer))
    out_dir = tmp_dir / "output"

    result = process_file(src, out_dir, dry_run=False)

    assert result.pii_stripped is True, (
        f"classified {result.doc_type!r} and skipped the scrubber"
    )
    assert discharge_ids['ssn'] not in _read_deliverable_text(out_dir)


def test_no_identifier_anywhere_in_the_output_directory(tmp_dir, discharge_text,
                                                        discharge_ids):
    """R10. The output directory is the one thing that leaves the machine, so
    it is the trust boundary. This used to fail: pii.py recorded a
    sixty-character context window around every medium-confidence detection
    and pipeline.py wrote it to output/review/review_log.jsonl -- and a window
    around a flagged ZIP contains the SSN."""
    src = tmp_dir / "discharge_summary.txt"
    src.write_text(discharge_text('copyright'))
    out_dir = tmp_dir / "output"
    process_file(src, out_dir, dry_run=False)
    written = _read_all_output_text(out_dir)
    for value in discharge_ids.values():
        assert value not in written, f"{value!r} is inside the output directory"


def test_detection_runs_for_every_doc_type(tmp_dir, monkeypatch, discharge_text):
    """Assert on the detection call, not on the output: caselaw and published
    documents are not redacted, but they must still be looked at."""
    import pipeline

    seen = []
    real_strip_pii = pipeline.strip_pii

    def spy(text, source_filename, output_mode='finetune'):
        seen.append(source_filename)
        return real_strip_pii(text, source_filename, output_mode=output_mode)

    monkeypatch.setattr(pipeline, 'strip_pii', spy)

    samples = {
        'caselaw': "Smith v. Jones, 123 F.3d 456 (7th Cir. 2019)\n\nOPINION\n\nAFFIRMED.\n",
        'published': ("Chicago Bar Journal\nVol. 42, No. 3 - ISSN 0009-3157\n"
                      "Continuing Legal Education\n© 2023 Chicago Bar Association\n"),
        'uncertain': "Just some text with no legal signals at all.\n",
        'private': discharge_text('copyright'),
    }
    for name, text in samples.items():
        src = tmp_dir / f"{name}.txt"
        src.write_text(text)
        process_file(src, tmp_dir / "output", dry_run=True)

    assert sorted(seen) == sorted(f"{n}.txt" for n in samples)


def test_caselaw_passes_through_unredacted(tmp_dir, caselaw_text):
    """Detection running on an opinion must not start redacting it. The corpus
    job depends on party names and citations surviving verbatim."""
    src = tmp_dir / "smith_v_jones.txt"
    src.write_text(caselaw_text)
    out_dir = tmp_dir / "output"

    result = process_file(src, out_dir, dry_run=False)

    assert result.doc_type == 'caselaw'
    assert result.pii_stripped is False
    assert result.faker_substitutions == 0
    rag = out_dir / "rag" / "smith_v_jones.jsonl"
    assert rag.exists(), "a caselaw document must still produce RAG output"
    written = '\n'.join(json.loads(l)['text'] for l in rag.read_text().splitlines())
    for verbatim in ("Smith v. Jones", "123 F.3d 456", "AFFIRMED"):
        assert verbatim in written


# --- U7: floor, quarantine, and surviving one bad file ------------------------

def _deliverables(out_dir: Path) -> list:
    return [p for sub in ('rag', 'finetune')
            for p in (out_dir / sub).rglob('*') if p.is_file()] \
        if out_dir.exists() else []


def test_detection_at_the_floor_is_redacted(tmp_dir):
    """KTD9 lowers the floor to 0.30. An MBI with no label beside it scores
    exactly 0.30 -- presidio is honest that an MBI has no checksum -- so it
    was detected and then discarded by the old 0.50 floor."""
    import pii
    text = "The card showed 1EG4TE5MK73 clearly.\n"
    assert any(round(r.score, 2) == 0.30 for r in
               pii._analyze_chunked(pii._get_engines()[0], text)), "fixture drifted"
    result = pii.strip_pii(text, "card.txt", output_mode='rag')
    assert "1EG4TE5MK73" not in result.text


def test_label_with_no_resolvable_value_is_quarantined(tmp_dir):
    """Plan Q1: quarantine on a structural failure -- a recognised label whose
    value could not be read. Common in scanned records, and silence is the
    failure mode this plan exists to remove."""
    src = tmp_dir / "scanned_record.txt"
    src.write_text(
        "RIVERTON CLINIC\nIntake Form\n\n"
        "Patient: Wanda Ferris\n"
        "Medical Record Number: \n"
        "Seen for follow-up.\n"
    )
    out_dir = tmp_dir / "output"
    result = process_file(src, out_dir, dry_run=False)

    assert result.skipped is True
    assert result.skip_reason == 'unresolved_identifier_label'
    assert _deliverables(out_dir) == [], "a quarantined document still wrote output"
    assert (out_dir / "review" / "pii_queue" / "scanned_record.txt").exists()


def test_public_document_containing_a_signal_identifier_is_quarantined(tmp_dir):
    """KTD13: an SSN never legitimately appears in published case law, so
    finding one is evidence the classifier was wrong."""
    src = tmp_dir / "handout.txt"
    src.write_text(
        "Chicago Bar Journal\nVol. 42, No. 3 - ISSN 0009-3157\n"
        "Continuing Legal Education\n\n"
        "Social Security Number: 412-55-9083\n"
    )
    out_dir = tmp_dir / "output"
    result = process_file(src, out_dir, dry_run=False)

    assert result.doc_type == 'published'
    assert result.skipped is True
    assert result.skip_reason == 'pii_in_public_document'
    assert _deliverables(out_dir) == []


def test_opinion_with_only_ambient_hits_is_not_quarantined(tmp_dir, caselaw_text):
    """The other half of KTD13. Person, date and location saturate legal text:
    a genuine opinion must pass through untouched, or unconditional detection
    destroys the corpus the tool was built to produce."""
    src = tmp_dir / "smith_v_jones.txt"
    src.write_text(caselaw_text)
    out_dir = tmp_dir / "output"

    result = process_file(src, out_dir, dry_run=False)

    assert result.skipped is False
    assert result.pii_stripped is False
    assert (out_dir / "rag").exists()


def test_ordinary_medium_confidence_detections_do_not_quarantine(tmp_dir):
    """The common case has to keep flowing. Quarantining on any ambiguous
    detection would hold back most of a real batch."""
    src = tmp_dir / "note.txt"
    src.write_text(
        "RIVERTON FAMILY MEDICINE\nOffice Visit Note\n\n"
        "Patient: Wanda Ferris\nSeen 09/11/2025 in the Riverton clinic.\n"
        "Follow-up with Dr. Marcus Oyelaran in two weeks.\n"
    )
    out_dir = tmp_dir / "output"
    result = process_file(src, out_dir, dry_run=False)

    assert result.skipped is False
    assert result.pii_stripped is True


def test_one_corrupt_file_does_not_cost_the_batch_its_manifest(tmp_dir):
    inp, out_dir = tmp_dir / "input", tmp_dir / "output"
    inp.mkdir(parents=True)
    (inp / "good.txt").write_text("A routine clinic note about a follow-up visit.\n")
    # Passes the extension check and then fails on read.
    (inp / "broken.docx").write_bytes(b"this is not a docx at all")

    process_directory(inp, out_dir, dry_run=False)

    manifest = json.loads((out_dir / "provenance.json").read_text())
    reasons = {f['skip_reason'] for f in manifest['files'] if f['skipped']}
    assert 'processing_error' in reasons, (
        f"the corrupt file did not report as an error: {reasons}"
    )
    assert (out_dir / "summary.html").exists(), "the run lost its report"


def test_one_corrupt_file_does_not_abort_a_parallel_batch(tmp_dir):
    inp, out_dir = tmp_dir / "input", tmp_dir / "output"
    inp.mkdir(parents=True)
    for i in range(3):
        (inp / f"good_{i}.txt").write_text(f"A routine clinic note number {i}.\n")
    (inp / "broken.docx").write_bytes(b"this is not a docx at all")

    process_directory(inp, out_dir, dry_run=False, workers=2)

    manifest = json.loads((out_dir / "provenance.json").read_text())
    assert manifest['summary']['total_files'] == 4
    assert manifest['summary']['processed_files'] == 3


def test_hold_backs_are_reported_separately_by_reason(tmp_dir):
    inp, out_dir = tmp_dir / "input", tmp_dir / "output"
    inp.mkdir(parents=True)
    (inp / "broken.docx").write_bytes(b"not a docx")
    (inp / "unreadable.txt").write_text(
        "Intake Form\nPatient: Wanda Ferris\nMedical Record Number: \nSeen today.\n"
    )
    process_directory(inp, out_dir, dry_run=False)

    summary = json.loads((out_dir / "provenance.json").read_text())['summary']
    assert summary['held_back']['processing_error'] == 1
    assert summary['held_back']['unresolved_identifier_label'] == 1

    html = (out_dir / "summary.html").read_text()
    from utils import HOLD_BACK_LABELS
    for reason in ('processing_error', 'unresolved_identifier_label'):
        assert HOLD_BACK_LABELS[reason] in html, f"{reason} has no label in the report"
    assert 'OCR queue (low confidence)' in html, "the OCR label was lost"


def test_dry_run_reports_a_quarantine_and_writes_nothing(tmp_dir, capsys):
    src = tmp_dir / "scanned_record.txt"
    src.write_text("Intake Form\nMedical Record Number: \nSeen today.\n")
    out_dir = tmp_dir / "output"

    result = process_file(src, out_dir, dry_run=True)

    assert result.skipped is True
    assert result.skip_reason == 'unresolved_identifier_label'
    assert not out_dir.exists(), "a dry run wrote to the output directory"


def test_nothing_detected_above_the_floor_survives_in_the_output():
    """KTD9's ordering note: containment resolution runs before score
    thresholding, so a high-scoring span can absorb a contained weaker one and
    then itself be dropped. Whatever the ordering, anything the tool detected
    at or above the floor must not still be in the text."""
    import pii
    from pathlib import Path as _Path
    corpus = _Path(__file__).parent / 'fixtures' / 'corpus'
    for document in sorted(corpus.glob('*.txt')):
        text = document.read_text()
        detections = pii._clamp_over_long_spans(
            text, pii._analyze_chunked(pii._get_engines()[0], text))
        scrubbed = pii.strip_pii(text, document.name, output_mode='rag').text
        for d in detections:
            if d.score >= pii.LOW_CONFIDENCE:
                original = text[d.start:d.end]
                assert original not in scrubbed, (
                    f"{document.name}: {d.entity_type} {original!r} scored "
                    f"{d.score:.2f} and was still left in the output"
                )


def test_slip_opinion_still_gets_rag_output(tmp_dir):
    """A court opinion whose only caselaw signal is the OPINION/AFFIRMED keyword
    must stay caselaw -- 'uncertain' would scrub it and drop it from the corpus."""
    src = tmp_dir / "slip_opinion.txt"
    src.write_text(
        "IN THE APPELLATE COURT OF ILLINOIS\n"
        "FIRST JUDICIAL DISTRICT\n\n"
        "OPINION\n\n"
        "Justice Elena Marsh delivered the judgment of the court.\n"
        "The circuit court's order is AFFIRMED.\n"
    )
    out_dir = tmp_dir / "output"

    result = process_file(src, out_dir, dry_run=False)

    assert result.doc_type == 'caselaw'
    assert result.pii_stripped is False
    assert (out_dir / "rag" / "slip_opinion.jsonl").exists()
