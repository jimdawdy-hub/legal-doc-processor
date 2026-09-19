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


# --- U2: a single weak signal must not carry a whole classification ----------
# Confidence used to be each category's share of the score that actually fired,
# so one signal firing alone normalised to 1.00 no matter how weak it was. A
# copyright footer on a discharge summary therefore read as "published, 100%",
# and published documents never reached the scrubber.

@pytest.mark.parametrize('footer', ['copyright', 'bar_association', 'issn'])
def test_single_published_signal_does_not_classify_published(discharge_text, footer):
    result = classify(Path("discharge_summary.pdf"), discharge_text(footer))
    assert result.doc_type != "published", (
        f"a lone {footer!r} line classified a medical record as published "
        f"at {result.confidence:.2f} confidence, routing it around the scrubber"
    )

def test_single_weak_published_signal_reports_low_confidence(discharge_text):
    # The copyright footer is the weakest published signal there is (0.20).
    result = classify(Path("discharge_summary.pdf"), discharge_text('copyright'))
    assert result.confidence < 0.60

def test_confidence_is_share_of_what_the_category_could_score(published_text):
    # A law review article firing every published signal is the only thing that
    # should read as a certainty.
    result = classify(Path("cle_article.pdf"), published_text)
    assert result.doc_type == "published"
    assert result.confidence == pytest.approx(1.00, abs=0.01)

def test_opinion_keyword_alone_still_classifies_caselaw():
    # A slip opinion with no reporter citation yet. Demoting this to 'uncertain'
    # would scrub the judges' and parties' names out of the case-law corpus.
    text = (
        "IN THE APPELLATE COURT OF ILLINOIS\n"
        "FIRST JUDICIAL DISTRICT\n\n"
        "OPINION\n\n"
        "The circuit court's order is AFFIRMED.\n"
    )
    result = classify(Path("slip_opinion.pdf"), text)
    assert result.doc_type == "caselaw"
