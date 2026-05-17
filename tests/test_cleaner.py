import pytest
from cleaner import clean

def test_collapses_excess_blank_lines():
    text = "First paragraph.\n\n\n\n\nSecond paragraph."
    result = clean(text)
    assert "\n\n\n" not in result
    assert "First paragraph." in result
    assert "Second paragraph." in result

def test_removes_leading_line_numbers():
    text = "1  The plaintiff filed suit.\n2  The defendant answered.\n10 The court ruled."
    result = clean(text)
    assert "1  The" not in result
    assert "The plaintiff filed suit." in result
    assert "The defendant answered." in result

def test_normalizes_smart_quotes():
    text = "“This is quoted.” She said ‘hello.’"
    result = clean(text)
    assert '"This is quoted."' in result
    assert "'hello.'" in result

def test_strips_repeated_header_lines():
    header = "CONFIDENTIAL — ATTORNEY WORK PRODUCT"
    text = f"{header}\n\nPage one content.\n\n{header}\n\nPage two content.\n\n{header}\n\nPage three content."
    result = clean(text)
    assert result.count(header) == 0
    assert "Page one content." in result

def test_normalizes_em_dashes():
    text = "The case—Smith v. Jones—was decided in 2019."
    result = clean(text)
    assert "—" not in result
    assert "The case--Smith v. Jones--was decided in 2019." in result

def test_strips_westlaw_annotation_lines():
    text = "Valid legal text.\n© 2023 Thomson Reuters. No claim to original U.S. Government Works.\nMore valid text."
    result = clean(text)
    assert "Thomson Reuters" not in result
    assert "Valid legal text." in result
    assert "More valid text." in result
