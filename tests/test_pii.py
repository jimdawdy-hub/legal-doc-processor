import re
from datetime import datetime

import pytest
from presidio_analyzer import RecognizerResult

import pii as pii_module
from pii import strip_pii, PIIResult


def _with_detections(monkeypatch, detections):
    """Pin the analyzer's output so replacement can be tested on its own.

    Replacement defects are about spans and offsets, not about detection, and
    spaCy's span boundaries are not stable enough to reproduce them through the
    real analyzer.
    """
    monkeypatch.setattr(
        pii_module, '_analyze_chunked', lambda analyzer, text: list(detections)
    )

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


# --- U3: replacement goes through the library ---------------------------------
# The hand-rolled replacement dropped the loser of an overlap in one mode and
# spliced over it in the other. Measured on the text below, finetune mode
# produced 'Patient Norma Fisher-9083 end' (four digits of the SSN survived) and
# placeholder mode produced 'Patient [PERSON]N] end' (corrupted).

OVERLAP_TEXT = "Patient Harold Vance 412-55-9083 end"
# A PERSON span that runs past the name and into the SSN -- the shape spaCy
# actually produced on the discharge summary.
OVERLAP_DETECTIONS = [
    RecognizerResult("PERSON", 8, 27, 0.85),
    RecognizerResult("US_SSN", 21, 32, 0.90),
]

def test_overlapping_detections_leave_no_fragment(monkeypatch):
    # Fragments chosen to be diagnostic rather than chance-sensitive: a
    # synthetic SSN is itself digits, so a two-digit run like '55' can reappear
    # by coincidence. '9083' is the tail the hand-rolled version actually left
    # behind, and the names cannot collide with a replacement value.
    _with_detections(monkeypatch, OVERLAP_DETECTIONS)
    result = strip_pii(OVERLAP_TEXT, "discharge.pdf", output_mode='finetune')
    for fragment in ("412-55-9083", "9083", "Harold", "Vance"):
        assert fragment not in result.text, (
            f"{fragment!r} survived replacement: {result.text!r}"
        )

def test_overlapping_detections_produce_well_formed_tokens(monkeypatch):
    _with_detections(monkeypatch, OVERLAP_DETECTIONS)
    result = strip_pii(OVERLAP_TEXT, "discharge.pdf", output_mode='rag')
    # Every bracket that opens must close, with a known entity type inside it.
    tokens = re.findall(r'\[[^\[\]]*\]', result.text)
    assert tokens, f"no tokens emitted: {result.text!r}"
    for token in tokens:
        assert token.strip('[]') in pii_module.ENTITY_TYPES, f"spliced token {token!r}"
    assert result.text.count('[') == result.text.count(']')
    # Placeholder tokens carry no digits, so this check is exact: any digit left
    # in the output came from the original identifier.
    assert not any(ch.isdigit() for ch in result.text), (
        f"digits of the original survived: {result.text!r}"
    )
    for fragment in ("Harold", "Vance"):
        assert fragment not in result.text

def test_repeated_name_gets_one_consistent_value(monkeypatch):
    text = "John Smith filed the motion. John Smith appeared in court."
    first, second = text.index("John"), text.rindex("John")
    _with_detections(monkeypatch, [
        RecognizerResult("PERSON", first, first + 10, 0.90),
        RecognizerResult("PERSON", second, second + 10, 0.90),
    ])
    result = strip_pii(text, "brief.pdf", output_mode='finetune')
    assert "John Smith" not in result.text
    before, _, after = result.text.partition(" filed the motion. ")
    assert after.endswith(" appeared in court.")
    assert before == after[:-len(" appeared in court.")], (
        f"one name became two different values: {result.text!r}"
    )

def test_same_name_in_two_documents_gets_different_values(monkeypatch):
    """R15: Faker.seed(0) was fixed at module level, so every document's first
    person became 'Norma Fisher'. A fixed scheme is inferable from a handful of
    outputs, which the de-identification literature treats as a re-identification
    path."""
    outputs = set()
    for filler in ("filed the motion.", "met the surgeon.", "signed the consent form."):
        text = f"John Smith {filler}"
        _with_detections(monkeypatch, [RecognizerResult("PERSON", 0, 10, 0.90)])
        outputs.add(strip_pii(text, "doc.pdf", output_mode='finetune').text[:-len(filler)])
    assert len(outputs) > 1, f"every document produced the same fake name: {outputs}"

def test_bare_year_is_not_replaced_by_a_full_date(monkeypatch):
    """R4: replacement must be type-correct. A model year routed through date
    handling came back as '2020-10-23'."""
    text = "The vehicle is a 2019 sedan."
    start = text.index("2019")
    _with_detections(monkeypatch, [RecognizerResult("DATE_TIME", start, start + 4, 0.90)])
    result = strip_pii(text, "claim.pdf", output_mode='finetune')
    replaced = result.text[len("The vehicle is a "):].split(" sedan")[0]
    assert re.fullmatch(r'\d{4}', replaced), (
        f"a bare year became {replaced!r}"
    )

# --- U4: over-long spans and chunk boundaries --------------------------------

# spaCy labelled this whole run -- URL, line break, and the next line's first
# word -- as one PERSON at 0.85, and replacement deleted all of it. The
# surrounding text gave no sign a line was missing.
OVER_LONG_TEXT = "See https://records.example.org/rfenwick48\nPhotograph on file."

def test_over_long_span_does_not_destroy_the_following_line(monkeypatch):
    start = OVER_LONG_TEXT.index("https")
    end = OVER_LONG_TEXT.index("Photograph") + len("Photograph")
    _with_detections(monkeypatch, [RecognizerResult("PERSON", start, end, 0.85)])
    result = strip_pii(OVER_LONG_TEXT, "record.pdf", output_mode='rag')
    assert "Photograph on file." in result.text, (
        f"the next line was swallowed by the span: {result.text!r}"
    )

def test_clamp_stops_a_span_at_the_start_of_new_text():
    start = OVER_LONG_TEXT.index("https")
    end = OVER_LONG_TEXT.index("Photograph") + len("Photograph")
    clamped = pii_module._clamp_span(OVER_LONG_TEXT, start, end)
    assert OVER_LONG_TEXT[start:clamped] == "https://records.example.org/rfenwick48"

def test_clamp_keeps_a_name_wrapped_across_a_soft_break():
    """PDF extraction wraps long names. Truncating at the break would leak the
    second line, so a genuine wrap must survive."""
    text = "Attending physician Harold\nVance signed the discharge order."
    start = text.index("Harold")
    end = text.index("Vance") + len("Vance")
    assert pii_module._clamp_span(text, start, end) == end

def test_wrapped_name_is_redacted_on_both_lines(monkeypatch):
    text = "Attending physician Harold\nVance signed the discharge order."
    start = text.index("Harold")
    end = text.index("Vance") + len("Vance")
    _with_detections(monkeypatch, [RecognizerResult("PERSON", start, end, 0.90)])
    result = strip_pii(text, "record.pdf", output_mode='rag')
    assert "Harold" not in result.text and "Vance" not in result.text
    assert "signed the discharge order." in result.text

def test_clamp_stops_a_span_crossing_two_line_breaks():
    """A name wraps once. Twice means the span has run into unrelated text."""
    text = "Harold\nVance\nPhotograph on file."
    end = text.index("Photograph") + len("Photograph")
    clamped = pii_module._clamp_span(text, 0, end)
    assert text[0:clamped] == "Harold\nVance"

@pytest.mark.parametrize('text,start,end', [
    (OVER_LONG_TEXT, OVER_LONG_TEXT.index("https"), len(OVER_LONG_TEXT)),
    ("Harold\nVance signed it.", 0, len("Harold\nVance")),
    ("no line breaks at all here", 3, 11),
    ("\nleading break", 0, 8),
    ("trailing break\n", 0, 15),
])
def test_clamp_never_extends_a_span(text, start, end):
    assert pii_module._clamp_span(text, start, end) <= end

def test_over_long_span_preserves_every_line(monkeypatch, lines_preserved):
    start = OVER_LONG_TEXT.index("https")
    end = OVER_LONG_TEXT.index("Photograph") + len("Photograph")
    _with_detections(monkeypatch, [RecognizerResult("PERSON", start, end, 0.85)])
    result = strip_pii(OVER_LONG_TEXT, "record.pdf", output_mode='rag')
    lines_preserved(OVER_LONG_TEXT, result.text)

def test_identifier_straddling_a_chunk_boundary_is_caught_once(monkeypatch):
    """The fixed-width split cut mid-identifier and re-mapped offsets by hand,
    so an SSN across the cut was missed silently."""
    monkeypatch.setattr(pii_module, 'MAX_PRESIDIO_CHARS', 400)
    monkeypatch.setattr(pii_module, 'CHUNK_OVERLAP_CHARS', 120)
    filler = "The patient was seen in the outpatient clinic today. "
    head = (filler * 20)[:395]
    text = head + "412-55-9083" + " " + (filler * 5)
    assert text.index("412-55-9083") < 400 < text.index("412-55-9083") + 11, (
        "fixture must straddle the boundary"
    )
    result = strip_pii(text, "record.pdf", output_mode='rag')
    assert "412-55-9083" not in result.text
    assert result.text.count("[US_SSN]") == 1, (
        f"expected exactly one redaction, got {result.text.count('[US_SSN]')}"
    )

@pytest.mark.parametrize('mode', ['finetune', 'rag'])
def test_round_trip_preserves_every_line(discharge_text, lines_preserved, mode):
    """U4's verification, through the real analyzer rather than pinned spans:
    every line entering strip_pii is represented in its output."""
    from cleaner import clean
    source = clean(discharge_text('copyright'))
    result = strip_pii(source, "discharge_summary.pdf", output_mode=mode)
    lines_preserved(source, result.text)

def test_dates_keep_their_format_and_their_interval(monkeypatch):
    text = "Admitted 03/01/2025 and discharged 03/15/2025."
    a, b = text.index("03/01/2025"), text.index("03/15/2025")
    _with_detections(monkeypatch, [
        RecognizerResult("DATE_TIME", a, a + 10, 0.90),
        RecognizerResult("DATE_TIME", b, b + 10, 0.90),
    ])
    result = strip_pii(text, "discharge.pdf", output_mode='finetune')
    found = re.findall(r'\d{2}/\d{2}/\d{4}', result.text)
    assert len(found) == 2, f"date format not preserved: {result.text!r}"
    assert found[0] != "03/01/2025" and found[1] != "03/15/2025"
    shifted = [datetime.strptime(d, "%m/%d/%Y") for d in found]
    assert (shifted[1] - shifted[0]).days == 14, "clinical interval not preserved"
