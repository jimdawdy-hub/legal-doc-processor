import pytest
from pii import strip_pii, PIIResult

SAMPLE_PRIVATE = (
    "James Kowalski filed a motion. His SSN is 123-45-6789. "
    "Call him at (312) 555-0100 or james.kowalski@lawfirm.com. "
    "He lives at 123 Main Street, Chicago, IL 60601."
)

def test_strip_pii_returns_pii_result():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='finetune')
    assert isinstance(result, PIIResult)

def test_finetune_mode_replaces_with_faker_not_tokens():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='finetune')
    assert "123-45-6789" not in result.text
    assert "james.kowalski@lawfirm.com" not in result.text
    assert "[US_SSN]" not in result.text
    assert "[EMAIL_ADDRESS]" not in result.text

def test_rag_mode_replaces_with_tokens():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='rag')
    assert "123-45-6789" not in result.text
    assert "[" in result.text

def test_substitution_count_is_positive():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='finetune')
    assert result.substitutions > 0

def test_medium_confidence_entities_go_to_review_log():
    result = strip_pii(SAMPLE_PRIVATE, "brief.pdf", output_mode='finetune')
    assert isinstance(result.review_flags, list)

def test_review_flag_has_required_fields():
    result = strip_pii(SAMPLE_PRIVATE, "brief.pdf", output_mode='finetune')
    for flag in result.review_flags:
        assert 'file' in flag
        assert 'entity_type' in flag
        assert 'original_text' in flag
        assert 'confidence' in flag
        assert 'context' in flag
        assert 'action' in flag

def test_consistent_faker_replacement_within_document():
    text = "John Smith filed the motion. John Smith appeared in court."
    result = strip_pii(text, "test.pdf", output_mode='finetune')
    assert "John Smith" not in result.text

def test_clean_text_unchanged():
    text = "The statute provides that courts shall apply a reasonableness standard."
    result = strip_pii(text, "statute.pdf", output_mode='finetune')
    assert result.substitutions == 0
    assert result.text == text
