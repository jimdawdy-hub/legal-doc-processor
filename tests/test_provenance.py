import csv
import json
import pytest
from pathlib import Path
from provenance import ProvenanceManifest

def _write_sidecar(tmp_dir: Path) -> Path:
    sidecar = tmp_dir / "sources.csv"
    with open(sidecar, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['filename', 'copyright_status', 'source_url_or_collection', 'notes'])
        writer.writeheader()
        writer.writerow({
            'filename': 'opinion.pdf',
            'copyright_status': 'public domain',
            'source_url_or_collection': 'https://courtlistener.com/1',
            'notes': '',
        })
    return sidecar

def _add_sample_file(manifest: ProvenanceManifest, filename: str = "opinion.pdf", **kwargs):
    defaults = dict(
        original_path=Path(filename),
        doc_type='caselaw',
        classification_confidence=0.91,
        ocr=False,
        ocr_confidence=None,
        pii_stripped=False,
        faker_substitutions=0,
        review_flags=0,
        chunk_count=12,
        token_count=5800,
    )
    defaults.update(kwargs)
    manifest.add_file(**defaults)

def test_save_creates_provenance_json(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m)
    m.save()
    assert (tmp_dir / 'provenance.json').exists()

def test_provenance_has_dataset_level_fields(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    for field in ('dataset_name', 'version', 'created', 'license', 'jurisdiction_coverage'):
        assert field in data

def test_provenance_preserves_dataset_fields_on_rerun(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    data['dataset_name'] = 'My Legal Dataset'
    (tmp_dir / 'provenance.json').write_text(json.dumps(data))
    m2 = ProvenanceManifest(tmp_dir)
    _add_sample_file(m2, "second.pdf")
    m2.save()
    data2 = json.loads((tmp_dir / 'provenance.json').read_text())
    assert data2['dataset_name'] == 'My Legal Dataset'

def test_provenance_per_file_record_structure(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    record = data['files'][0]
    assert 'original_filename' in record
    assert 'sha256' in record
    assert 'doc_type' in record
    assert 'date_processed' in record
    assert 'processing' in record
    assert 'chunk_count' in record['processing']
    assert 'token_count' in record['processing']

def test_sidecar_populates_copyright_fields(tmp_dir):
    sidecar = _write_sidecar(tmp_dir)
    (tmp_dir / 'opinion.pdf').write_bytes(b'fake pdf content')
    m = ProvenanceManifest(tmp_dir, sidecar_path=sidecar)
    m.add_file(
        original_path=tmp_dir / 'opinion.pdf',
        doc_type='caselaw', classification_confidence=0.91,
        ocr=False, ocr_confidence=None, pii_stripped=False,
        faker_substitutions=0, review_flags=0, chunk_count=5, token_count=1000,
    )
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    record = data['files'][0]
    assert record['copyright_status'] == 'public domain'
    assert record['source_url_or_collection'] == 'https://courtlistener.com/1'

def test_summary_counts_are_correct(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m, 'case1.pdf', doc_type='caselaw')
    _add_sample_file(m, 'case2.pdf', doc_type='caselaw')
    _add_sample_file(m, 'email.pdf', doc_type='private', pii_stripped=True, faker_substitutions=3)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    assert data['summary']['caselaw_files'] == 2
    assert data['summary']['private_files'] == 1
    assert data['summary']['total_files'] == 3
