"""Nothing in the output directory reveals which document it came from (U12).

R16, R10. Three writers put identifiers and source identities into the output
directory without passing through the scrubber, so no text-based check would
ever have caught them.
"""
import json
from pathlib import Path

import pytest

from pipeline import process_directory
from writer import records_root

# Distinctive enough that a coincidental match is not plausible.
CLIENT_FOLDER = "Broadwater-Cecil-2025"
DOC_STEM = "Broadwater-discharge"


@pytest.fixture(autouse=True)
def isolated_records(tmp_path, monkeypatch):
    monkeypatch.setenv('LEGAL_DOC_RECORDS_DIR', str(tmp_path / 'records'))


def _run(tmp_dir, discharge_text, folder=CLIENT_FOLDER, stem=DOC_STEM):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    (inp / folder).mkdir(parents=True, exist_ok=True)
    (inp / folder / f"{stem}.txt").write_text(discharge_text('copyright'))
    process_directory(inp, out, dry_run=False)
    return inp, out


def _all_output_text(out_dir: Path) -> str:
    return '\n'.join(p.read_text(errors='replace')
                     for p in out_dir.rglob('*') if p.is_file())


def _identity_fragments(inp: Path) -> list:
    """Every filename, stem and path component of the input."""
    fragments = {CLIENT_FOLDER, DOC_STEM, f"{DOC_STEM}.txt", "Broadwater", "Cecil"}
    fragments.update(part for part in inp.resolve().parts if len(part) > 3)
    return sorted(fragments)


def test_no_file_in_the_output_directory_names_its_source(tmp_dir, discharge_text):
    inp, out = _run(tmp_dir, discharge_text)
    written = _all_output_text(out)
    names = '\n'.join(str(p.relative_to(out)) for p in out.rglob('*'))
    for fragment in _identity_fragments(inp):
        assert fragment not in written, f"{fragment!r} is inside an output file"
        assert fragment not in names, f"{fragment!r} is in an output path"


def test_provenance_carries_no_filename_path_or_folder(tmp_dir, discharge_text):
    """Three source-identifying fields, not two: original_filename and the
    resolved source_path per file, and -- easily missed -- a manifest-level
    source_dir holding the resolved input directory."""
    inp, out = _run(tmp_dir, discharge_text)
    manifest = json.loads((out / "provenance.json").read_text())

    assert 'source_dir' not in manifest
    for record in manifest['files']:
        assert 'original_filename' not in record
        assert 'source_path' not in record
        assert record['anon_id'].startswith('anon_')

    blob = json.dumps(manifest)
    for fragment in _identity_fragments(inp):
        assert fragment not in blob


def test_rag_output_is_keyed_on_the_anonymous_id(tmp_dir, caselaw_text):
    inp, out = tmp_dir / "input", tmp_dir / "output"
    (inp / CLIENT_FOLDER).mkdir(parents=True)
    (inp / CLIENT_FOLDER / f"{DOC_STEM}.txt").write_text(caselaw_text)
    process_directory(inp, out, dry_run=False)

    rag_files = list((out / "rag").glob("*.jsonl"))
    assert len(rag_files) == 1
    assert DOC_STEM.lower() not in rag_files[0].name.lower()

    for line in rag_files[0].read_text().splitlines():
        record = json.loads(line)
        assert DOC_STEM.lower() not in record['id'].lower()
        assert record['metadata']['source'].startswith('anon_')
        assert DOC_STEM.lower() not in record['metadata']['source'].lower()


def test_two_folders_sharing_a_filename_produce_two_rag_files(tmp_dir, caselaw_text):
    """They used to overwrite each other's RAG output -- the same hazard U6
    fixed for the other artifacts."""
    inp, out = tmp_dir / "input", tmp_dir / "output"
    for folder in ("client_a", "client_b"):
        (inp / folder).mkdir(parents=True)
        (inp / folder / "opinion.txt").write_text(
            caselaw_text + f"\nFiled in the {folder} matter.\n"
        )
    process_directory(inp, out, dry_run=False)

    rag_files = list((out / "rag").glob("*.jsonl"))
    assert len(rag_files) == 2, (
        f"one file overwrote the other: {[p.name for p in rag_files]}"
    )


def test_the_mapping_back_to_the_filename_is_recoverable(tmp_dir, discharge_text):
    """A human holding both artifacts can still trace a document end to end --
    the evidence record keeps the mapping, outside the output directory."""
    inp, out = _run(tmp_dir, discharge_text)
    anon_ids = {f['anon_id'] for f in json.loads((out / "provenance.json").read_text())['files']}

    recovered = {}
    for record_path in (records_root() / 'evidence').glob('*.json'):
        record = json.loads(record_path.read_text())
        recovered[record['anon_id']] = record['source']

    for anon_id in anon_ids:
        assert anon_id in recovered
        assert recovered[anon_id]['filename'] == f"{DOC_STEM}.txt"
        assert CLIENT_FOLDER in recovered[anon_id]['directory']


def test_an_output_directory_from_before_this_change_is_reported(tmp_dir, capsys):
    """Assumptions call those directories disposable, but disposable is not
    deleted: absent an explicit check the plan can be complete while the leak
    still sits on disk."""
    from pipeline import legacy_output_directories

    stale = tmp_dir / "old_output"
    stale.mkdir()
    (stale / "provenance.json").write_text(json.dumps({
        'files': [{
            'original_filename': 'Broadwater-discharge.pdf',
            'source_path': '/home/jim/cases/Broadwater-Cecil-2025/discharge.pdf',
        }],
        'source_dir': '/home/jim/cases/Broadwater-Cecil-2025',
    }))

    found = legacy_output_directories([tmp_dir])
    assert stale in found

    current = tmp_dir / "new_output"
    current.mkdir()
    (current / "provenance.json").write_text(json.dumps({
        'files': [{'anon_id': 'anon_abc123', 'doc_key': 'abc'}],
    }))
    assert current not in legacy_output_directories([tmp_dir])


# --- the adversarial report -----------------------------------------------

def test_the_risk_report_is_written_outside_the_output_directory(tmp_dir):
    """By construction it holds text just proved to be identifying."""
    import re_id_risk

    out = tmp_dir / "output"
    out.mkdir()
    path = re_id_risk.risk_report_path(out, batch_id='batch-xyz')
    assert out not in path.parents and path.parent != out
    assert records_root() in path.parents


def test_second_pass_finds_the_report_in_its_new_location(tmp_dir):
    """re_id_risk.py --apply chains straight into run_second_pass, which used
    to hardcode output_dir / 're_id_risk_report.json' and exit 1 without it."""
    import re_id_risk
    import second_pass

    out = tmp_dir / "output"
    (out / "finetune").mkdir(parents=True)
    (out / "finetune" / "dataset.jsonl").write_text(json.dumps({
        'text': 'Seen by Dr. Marcus Oyelaran at Riverton Clinic.',
        'metadata': {'source': 'anon_abc123', 'doc_type': 'private'},
    }) + '\n')

    # Both sides resolve the location the same way -- that is the contract.
    report_path = re_id_risk.risk_report_path(out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        'generated_at': '2026-09-19T00:00:00Z',
        'risk_distribution': {'HIGH': 1},
        'highest_risk_level': 'HIGH',
        'assessments': [{
            '_source': 'anon_abc123',
            'risk_level': 'HIGH',
            'risk_summary': 'A named clinician narrows the population.',
            'quasi_identifiers': [
                {'type': 'treating_physician', 'value': 'Marcus Oyelaran'}
            ],
            'recommendations': ['Redact the clinician name.'],
        }],
    }))

    result = second_pass.run_second_pass(out, quiet=True)
    assert result['total_substitutions'] >= 1
    patched = (out / "finetune" / "dataset.jsonl").read_text()
    assert 'Marcus Oyelaran' not in patched


def test_the_html_risk_section_carries_counts_but_no_quoted_text(tmp_dir):
    import re_id_risk

    out = tmp_dir / "output"
    out.mkdir()
    (out / "summary.html").write_text(
        "<!DOCTYPE html>\n<html><body><h1>Report</h1>\n</body>\n</html>\n"
    )
    report = {
        'generated_at': '2026-09-19T00:00:00Z',
        'model': 'test-model',
        'records_assessed': 1,
        'total_records_in_dataset': 1,
        'risk_distribution': {'HIGH': 1},
        'highest_risk_level': 'HIGH',
        'assessments': [{
            '_source': 'anon_abc123',
            'risk_level': 'HIGH',
            'risk_summary': 'A named clinician narrows the population.',
            'quasi_identifiers': [
                {'type': 'treating_physician', 'value': 'Marcus Oyelaran'}
            ],
            'recommendations': ['Redact the clinician name.'],
        }],
    }
    re_id_risk._append_risk_section(out / "summary.html", report)

    html = (out / "summary.html").read_text()
    assert 'HIGH' in html
    assert 'treating_physician' in html
    assert 'Marcus Oyelaran' not in html, (
        "the risk section quoted the surviving identifier it just flagged"
    )
    assert '<!-- re-id-risk-section -->' in html
