"""Synthetic-only regressions for the privacy and data-loss review findings."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from presidio_analyzer import RecognizerResult
import pii
from pipeline import process_file, process_directory, _select_best_versions
from utils import atomic_text, document_id
from cleaner import clean
from chunker import count_tokens


def entity(start, end, kind='PERSON'):
    return RecognizerResult(kind, start, end, 0.9)


@pytest.mark.parametrize('mode', ['rag', 'finetune'])
def test_overlapping_entities_leave_no_detected_tail(mode):
    text = 'before ABCDEFGHIJ after'
    analyzer = SimpleNamespace(analyze=lambda **_: [entity(7, 13), entity(10, 17)])
    with patch.object(pii, '_get_engines', return_value=(analyzer, None)):
        result = pii.strip_pii(text, 'synthetic', output_mode=mode)
    assert result.text.startswith('before ')
    assert result.text.endswith(' after')
    assert 'GHIJ' not in result.text
    assert result.substitutions == 1


def test_identifier_spanning_analysis_boundary_is_found(monkeypatch):
    monkeypatch.setattr(pii, 'MAX_PRESIDIO_CHARS', 1000)
    marker = 'synthetic@example.invalid'
    text = 'x' * 990 + ' ' + marker + ' end'
    def analyze(text, **kwargs):
        start = text.find(marker)
        return [] if start < 0 else [entity(start, start + len(marker), 'EMAIL_ADDRESS')]
    results = pii._analyze_chunked(SimpleNamespace(analyze=analyze), text)
    assert len(results) == 1
    assert text[results[0].start:results[0].end] == marker


def test_labeled_medical_and_case_identifiers_are_removed():
    text = 'MRN: SYN-91824; Member ID: SYN-78123; Case No.: SYN-2026-987'
    with patch.object(pii, '_get_engines', return_value=(SimpleNamespace(analyze=lambda **_: []), None)):
        result = pii.strip_pii(text, 'synthetic', output_mode='rag')
    for raw in ['SYN-91824', 'SYN-78123', 'SYN-2026-987']:
        assert raw not in result.text
    assert 'MRN:' in result.text


def test_invalid_detected_date_is_not_returned_unchanged():
    assert pii._shift_date('not-a-date', 200) == '[DATE]'


def test_model_is_never_downloaded_implicitly(monkeypatch):
    monkeypatch.setattr(pii, '_analyzer', None)
    with patch('spacy.util.is_package', return_value=False), patch.object(pii, 'AnalyzerEngine') as factory:
        with pytest.raises(RuntimeError, match='automatic model download'):
            pii._get_engines()
        factory.assert_not_called()


def test_copyright_footer_cannot_exempt_patient_text(tmp_path):
    source = tmp_path / 'Synthetic-Patient-File.txt'
    source.write_text('Medical handout\nSSN: 123-45-6789\n© 2024 Example Hospital\n')
    result = process_file(source, tmp_path / 'out')
    assert result.doc_type == 'published'  # classification is not privacy authority
    assert result.pii_stripped
    for folder in ['finetune', 'rag']:
        for path in (tmp_path / 'out' / folder).glob('*.jsonl'):
            assert source.name not in path.read_text()
            assert '123-45-6789' not in path.read_text()
            assert path.stat().st_mode & 0o777 == 0o600


def test_same_named_documents_are_not_silently_discarded(tmp_path):
    paths = [tmp_path / 'a' / 'record.txt', tmp_path / 'b' / 'record.txt', tmp_path / 'a' / 'record.pdf']
    for i, path in enumerate(paths):
        path.parent.mkdir(exist_ok=True)
        path.write_text(f'different synthetic content {i}')
    assert set(_select_best_versions(paths, [])) == set(paths)


def test_only_exact_duplicate_bytes_are_deduplicated(tmp_path):
    paths = [tmp_path / 'a.txt', tmp_path / 'b.txt']
    for path in paths:
        path.write_text('same bytes')
    skipped = []
    assert len(_select_best_versions(paths, skipped)) == 1
    assert skipped[0]['reason'] == 'exact_duplicate'


def test_document_id_survives_a_new_python_hash_seed(tmp_path):
    path = tmp_path / 'synthetic.txt'
    script = 'from utils import document_id; from pathlib import Path; import sys; print(document_id(Path(sys.argv[1])))'
    value = subprocess.check_output([sys.executable, '-c', script, str(path)],
                                    env={**os.environ, 'PYTHONHASHSEED': '987'}, text=True).strip()
    assert value == document_id(path)
    assert document_id(tmp_path / 'other' / path.name) != value


def test_reusing_output_refuses_before_modifying_prior_data(tmp_path):
    inp, out = tmp_path / 'input', tmp_path / 'out'
    inp.mkdir(); out.mkdir()
    original = out / 'provenance.json'
    original.write_text('{"prior": true}')
    with pytest.raises(ValueError, match='must be empty'):
        process_directory(inp, out)
    assert original.read_text() == '{"prior": true}'


def test_output_inside_input_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='must not overlap'):
        process_directory(tmp_path, tmp_path / 'out', dry_run=True)


def test_corrupt_document_preserves_batch_manifest_and_fails(tmp_path):
    inp, out = tmp_path / 'input', tmp_path / 'out'
    inp.mkdir()
    (inp / 'bad.pdf').write_bytes(b'not a PDF')
    (inp / 'good.txt').write_text('Synthetic body with SSN: 123-45-6789')
    with pytest.raises(RuntimeError, match='failed'):
        process_directory(inp, out)
    data = json.loads((out / 'provenance.json').read_text())
    assert data['summary']['processed_files'] == 1
    assert data['summary']['skipped_files'] == 1
    assert '123-45-6789' not in (out / 'finetune' / 'dataset.jsonl').read_text()


def test_private_records_are_actually_chunked(tmp_path):
    source = tmp_path / 'long.txt'
    source.write_text('A synthetic discussion of procedural issues. ' * 1200)
    process_file(source, tmp_path / 'out')
    rows = [json.loads(line) for line in (tmp_path / 'out/finetune/dataset.jsonl').read_text().splitlines()]
    assert len(rows) > 1
    assert [r['metadata']['chunk_index'] for r in rows] == list(range(len(rows)))
    assert all(count_tokens(r['text']) <= 2048 for r in rows)


def test_atomic_write_does_not_truncate_on_failure(tmp_path):
    path = tmp_path / 'data.jsonl'
    path.write_text('old data')
    with pytest.raises(RuntimeError):
        with atomic_text(path) as stream:
            stream.write('incomplete new data')
            raise RuntimeError('simulated failure')
    assert path.read_text() == 'old data'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['data.jsonl']


def test_cleaner_keeps_repeated_substantive_lines_and_addresses():
    text = '123 Main Street\nNo evidence of fracture.\n' * 3
    result = clean(text)
    assert result.count('123 Main Street') == 3
    assert result.count('No evidence of fracture.') == 3


def test_tokenizer_treats_special_token_text_as_data():
    assert count_tokens('Quoted marker <|endoftext|> in evidence') > 0


def test_medium_confidence_overlap_remains_visible_for_human_review():
    analyzer = SimpleNamespace(analyze=lambda **_: [
        RecognizerResult('PERSON', 0, 5, 0.9), RecognizerResult('PERSON', 3, 8, 0.7)])
    with patch.object(pii, '_get_engines', return_value=(analyzer, None)):
        result = pii.strip_pii('ABCDEFGH tail', 'synthetic', output_mode='rag')
    assert result.text == '[PERSON] tail'
    assert len(result.review_flags) == 1
    assert result.review_flags[0]['original_text'] == 'DEFGH'
