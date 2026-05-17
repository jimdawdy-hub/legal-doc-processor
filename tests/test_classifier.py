import pytest
from pathlib import Path
from classifier import classify

def test_classifies_caselaw(caselaw_text):
    result = classify(Path("smith_v_jones.pdf"), caselaw_text)
    assert result.doc_type == "caselaw"
    assert result.confidence >= 0.60

def test_classifies_published(published_text):
    result = classify(Path("cle_article.pdf"), published_text)
    assert result.doc_type == "published"
    assert result.confidence >= 0.60

def test_classifies_private(private_text):
    result = classify(Path("client_brief.pdf"), private_text)
    assert result.doc_type == "private"
    assert result.confidence >= 0.60

def test_eml_extension_always_private():
    result = classify(Path("message.eml"), "any text here")
    assert result.doc_type == "private"
    assert result.confidence == 0.99

def test_msg_extension_always_private():
    result = classify(Path("email.msg"), "any text here")
    assert result.doc_type == "private"
    assert result.confidence == 0.99

def test_pptx_extension_always_published():
    result = classify(Path("cle_presentation.pptx"), "any text here")
    assert result.doc_type == "published"
    assert result.confidence == 0.90

def test_uncertain_when_no_signals():
    result = classify(Path("mystery.pdf"), "This is just some random text with no legal signals.")
    assert result.doc_type == "uncertain"

def test_email_headers_in_body_classify_private():
    text = "To: john@example.com\nFrom: jane@example.com\nSubject: Re: Case update\n\nBody of email."
    result = classify(Path("printed_email.pdf"), text)
    assert result.doc_type == "private"

def test_attorney_for_classifies_private():
    text = "IN THE CIRCUIT COURT\nATTORNEYS FOR PLAINTIFF: James Smith\nLAW DIVISION\nDocket No. 2024-L-001234"
    result = classify(Path("pleading.pdf"), text)
    assert result.doc_type == "private"

def test_signals_dict_populated(caselaw_text):
    result = classify(Path("opinion.pdf"), caselaw_text)
    assert "scores" in result.signals
    assert result.signals["scores"]["caselaw"] > 0
