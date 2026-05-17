import json
import pytest
from pathlib import Path
from writer import write_rag_chunks, write_finetune_record
from chunker import Chunk

def _make_chunks(n=3):
    return [
        Chunk(text=f"Chunk {i} text about the law.", index=i, total=n, token_count=10)
        for i in range(n)
    ]

def test_write_rag_chunks_creates_jsonl(tmp_dir):
    chunks = _make_chunks(3)
    write_rag_chunks(chunks, "smith_v_jones.pdf", "caselaw", {}, tmp_dir)
    out = tmp_dir / "smith_v_jones.jsonl"
    assert out.exists()

def test_rag_jsonl_has_correct_record_count(tmp_dir):
    chunks = _make_chunks(3)
    write_rag_chunks(chunks, "opinion.pdf", "caselaw", {}, tmp_dir)
    lines = (tmp_dir / "opinion.jsonl").read_text().strip().split('\n')
    assert len(lines) == 3

def test_rag_record_structure(tmp_dir):
    chunks = _make_chunks(2)
    write_rag_chunks(chunks, "case.pdf", "caselaw", {"citation": "Smith v. Jones, 123 F.3d 456"}, tmp_dir)
    record = json.loads((tmp_dir / "case.jsonl").read_text().split('\n')[0])
    assert "id" in record
    assert "text" in record
    assert "metadata" in record
    assert record["metadata"]["doc_type"] == "caselaw"
    assert record["metadata"]["citation"] == "Smith v. Jones, 123 F.3d 456"
    assert record["metadata"]["chunk_index"] == 0
    assert record["metadata"]["total_chunks"] == 2

def test_rag_id_is_unique_per_chunk(tmp_dir):
    chunks = _make_chunks(3)
    write_rag_chunks(chunks, "opinion.pdf", "caselaw", {}, tmp_dir)
    lines = (tmp_dir / "opinion.jsonl").read_text().strip().split('\n')
    ids = [json.loads(l)["id"] for l in lines]
    assert len(set(ids)) == 3

def test_write_finetune_record_appends(tmp_dir):
    out_file = tmp_dir / "dataset.jsonl"
    write_finetune_record("First doc text.", "anon_001", "private", True, 3, 1, 50, out_file)
    write_finetune_record("Second doc text.", "anon_002", "private", True, 0, 0, 40, out_file)
    lines = out_file.read_text().strip().split('\n')
    assert len(lines) == 2

def test_finetune_record_structure(tmp_dir):
    out_file = tmp_dir / "dataset.jsonl"
    write_finetune_record("Legal text here.", "anon_042", "private", True, 5, 2, 120, out_file)
    record = json.loads(out_file.read_text())
    assert record["text"] == "Legal text here."
    assert record["metadata"]["source"] == "anon_042"
    assert record["metadata"]["pii_stripped"] is True
    assert record["metadata"]["faker_substitutions"] == 5
    assert record["metadata"]["review_flags"] == 2
    assert record["metadata"]["token_count"] == 120

def test_finetune_source_is_anon_id_not_real_filename(tmp_dir):
    out_file = tmp_dir / "dataset.jsonl"
    write_finetune_record("Text.", "anon_00042", "private", True, 0, 0, 10, out_file)
    record = json.loads(out_file.read_text())
    assert "real_filename" not in record["metadata"]["source"]
    assert record["metadata"]["source"].startswith("anon_")
