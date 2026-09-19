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


@pytest.mark.xfail(strict=True, reason=(
    "R10, owned by U8: pii.py records a 60-character context window around "
    "every medium-confidence detection, and pipeline.py writes it to "
    "output/review/review_log.jsonl -- inside the directory that leaves the "
    "machine. A window around a flagged ZIP routinely contains the SSN. U8 "
    "moves the record out and strips original values and context excerpts; "
    "this marker is strict so it fails the moment that lands."
))
def test_no_identifier_anywhere_in_the_output_directory(tmp_dir, discharge_text,
                                                        discharge_ids):
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
