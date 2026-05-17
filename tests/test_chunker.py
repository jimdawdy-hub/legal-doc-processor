import pytest
from chunker import chunk, count_tokens, Chunk

def test_chunk_returns_list_of_chunk_objects(caselaw_text):
    result = chunk(caselaw_text, 'caselaw')
    assert isinstance(result, list)
    assert all(isinstance(c, Chunk) for c in result)

def test_chunk_indices_are_sequential(caselaw_text):
    result = chunk(caselaw_text * 10, 'caselaw')
    for i, c in enumerate(result):
        assert c.index == i
        assert c.total == len(result)

def test_chunk_token_counts_are_positive(caselaw_text):
    result = chunk(caselaw_text, 'caselaw')
    assert all(c.token_count > 0 for c in result)

def test_caselaw_chunks_at_512_tokens():
    long_text = "The court held that the defendant was liable. " * 300
    result = chunk(long_text, 'caselaw')
    assert len(result) > 1
    for c in result:
        assert c.token_count <= 600

def test_private_chunks_at_2048_tokens():
    short_text = "Client retained our firm to handle the matter. " * 20
    result = chunk(short_text, 'private')
    assert len(result) == 1

def test_count_tokens_returns_int():
    n = count_tokens("The court held for the plaintiff.")
    assert isinstance(n, int)
    assert n > 0

def test_single_chunk_covers_full_text():
    text = "Short legal text for a private document."
    result = chunk(text, 'private')
    assert len(result) == 1
    assert result[0].text.strip() == text.strip()
