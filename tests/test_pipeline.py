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
