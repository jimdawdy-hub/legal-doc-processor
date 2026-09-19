"""The report after the approval workflow is retired (U9).

R13: running the tool requires no per-document human approval. There was no
test coverage at all for reporter.py, review_server.py, apply_decisions.py or
review_pii.py, which is why these are written before the removal.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import process_directory
from reporter import generate_reports

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_records(tmp_path, monkeypatch):
    monkeypatch.setenv('LEGAL_DOC_RECORDS_DIR', str(tmp_path / 'records'))


def _run(tmp_dir, discharge_text) -> Path:
    inp, out = tmp_dir / "input", tmp_dir / "output"
    inp.mkdir(parents=True)
    (inp / "discharge_summary.txt").write_text(discharge_text('copyright'))
    process_directory(inp, out, dry_run=False)
    return out


# --- the deleted modules ------------------------------------------------------

@pytest.mark.parametrize('module', ['review_server.py', 'apply_decisions.py'])
def test_the_approval_modules_are_gone(module):
    assert not (REPO_ROOT / module).exists()


@pytest.mark.parametrize('module', ['review_server', 'apply_decisions'])
def test_nothing_imports_a_deleted_module(module):
    hits = subprocess.run(
        ['grep', '-rn', '--include=*.py', f'import {module}', str(REPO_ROOT)],
        capture_output=True, text=True,
    ).stdout.strip()
    assert not hits, f"still imported:\n{hits}"


# --- the report itself --------------------------------------------------------

def test_the_report_has_no_approval_controls(tmp_dir, discharge_text):
    out = _run(tmp_dir, discharge_text)
    html = (out / 'summary.html').read_text()
    for forbidden in ('fetch(', 'batchTypeBtn', 'batchFileBtn', 'batch-btn',
                      'Download Decisions CSV', 'apply_decisions',
                      'localhost', 'All correct', 'All wrong'):
        assert forbidden not in html, f"{forbidden!r} survived in the report"


def test_the_exported_csv_has_no_decision_column(tmp_dir, discharge_text):
    out = _run(tmp_dir, discharge_text)
    header = (out / 'review' / 'review_log.csv').read_text().splitlines()[0]
    assert 'decision' not in header
    assert 'original_text' not in header
    assert 'context' not in header


def test_the_report_renders_with_no_server_running(tmp_dir, discharge_text):
    """It is a static file. If it needed a server it would carry a script tag
    or a fetch call, and it carries neither."""
    out = _run(tmp_dir, discharge_text)
    html = (out / 'summary.html').read_text()
    assert html.lstrip().startswith('<!DOCTYPE html>')
    assert '<script' not in html
    assert html.rstrip().endswith('</html>')


def test_the_re_id_risk_splice_anchor_survives(tmp_dir, discharge_text):
    """re_id_risk.py appends its section by replacing the first '</body>'.
    The template rewrite would otherwise break it silently -- the coupling is
    invisible to imports and to any test exercising reporter.py alone."""
    out = _run(tmp_dir, discharge_text)
    html = (out / 'summary.html').read_text()
    assert html.count('</body>') == 1

    section = "<!-- re-id-risk-section -->\n<h2>Risk</h2>\n<!-- /re-id-risk-section -->"
    spliced = html.replace('</body>', section + '\n</body>', 1)
    assert '<!-- re-id-risk-section -->' in spliced
    assert spliced.index(section) < spliced.index('</body>')


def test_generate_reports_runs_from_both_callers(tmp_dir, discharge_text):
    """pipeline.py and second_pass.py both call it unconditionally, and
    neither passes a flag distinguishing them."""
    out = _run(tmp_dir, discharge_text)
    first = (out / 'summary.html').read_text()

    generate_reports(out)  # the second_pass.py call shape
    assert (out / 'summary.html').exists()
    assert 'Pipeline Report' in (out / 'summary.html').read_text()
    assert len(first) > 0


# --- review_pii.py ------------------------------------------------------------

def _review_pii(*args, records_dir: str):
    import os
    env = {**os.environ, 'LEGAL_DOC_RECORDS_DIR': records_dir}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / 'review_pii.py'), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _batch_id(records_dir: Path) -> str:
    batch_file = next((records_dir / 'verification').glob('*/batch.json'))
    return json.loads(batch_file.read_text())['batch_id']


def test_review_pii_summarises_the_verification_record(tmp_dir, discharge_text,
                                                       tmp_path):
    records_dir = tmp_path / 'records'
    out = _run(tmp_dir, discharge_text)
    batch = _batch_id(records_dir)

    result = _review_pii('--batch', batch, '--summary',
                         records_dir=str(records_dir))

    assert result.returncode == 0, result.stderr
    assert 'VERIFICATION RECORD' in result.stdout
    assert 'US_SSN' in result.stdout


def test_review_pii_refuses_a_csv_target_inside_the_output_directory(
        tmp_dir, discharge_text, tmp_path):
    """It writes original values, and dropping the export next to the
    deliverable is exactly how they would end up there."""
    records_dir = tmp_path / 'records'
    out = _run(tmp_dir, discharge_text)
    batch = _batch_id(records_dir)

    result = _review_pii('--batch', batch, '--csv', str(out / 'flags.csv'),
                         records_dir=str(records_dir))

    assert result.returncode != 0
    assert 'output directory' in result.stderr
    assert not (out / 'flags.csv').exists()


def test_review_pii_allows_a_csv_target_outside_the_output_directory(
        tmp_dir, discharge_text, tmp_path):
    records_dir = tmp_path / 'records'
    _run(tmp_dir, discharge_text)
    batch = _batch_id(records_dir)
    target = tmp_path / 'review_export.csv'

    result = _review_pii('--batch', batch, '--csv', str(target),
                         records_dir=str(records_dir))

    assert result.returncode == 0, result.stderr
    assert target.exists()


def test_review_pii_accepts_a_batch(tmp_dir, discharge_text, tmp_path):
    records_dir = tmp_path / 'records'
    _run(tmp_dir, discharge_text)
    batch = _batch_id(records_dir)

    result = _review_pii('--accept', batch, records_dir=str(records_dir))

    assert result.returncode == 0, result.stderr
    assert not (records_dir / 'verification' / batch).exists()
    assert list((records_dir / 'evidence').glob('*.json')), (
        "accepting a batch must not touch the evidence record"
    )
