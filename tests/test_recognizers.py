"""Acceptance tests for the identifiers presidio does not ship (U5).

These go through pii.py's configured engine rather than instantiating a
recognizer directly, so forgetting to register one fails the test.
"""
import re

import pytest

import pii as pii_module
import recognizers
from pii import strip_pii


def detect(text: str) -> list:
    """(entity_type, matched_text, score) for everything the engine finds."""
    analyzer, _ = pii_module._get_engines()
    found = analyzer.analyze(text=text, entities=pii_module.ENTITY_TYPES, language='en')
    return [(r.entity_type, text[r.start:r.end], r.score) for r in found]


def redactable(text: str, entity: str) -> list:
    """Matches for one entity type that are over the redaction floor."""
    return [
        matched for kind, matched, score in detect(text)
        if kind == entity and score >= pii_module.LOW_CONFIDENCE
    ]


# --- label-anchored identifiers (R5, KTD6) -----------------------------------

@pytest.mark.parametrize('label', recognizers.LABEL_SYNONYMS['MEDICAL_RECORD_NUMBER'])
def test_every_medical_record_label_synonym_is_detected(label):
    text = f"Patient summary.\n{label.title()}: 4417392\nSeen in clinic."
    assert '4417392' in redactable(text, 'MEDICAL_RECORD_NUMBER'), (
        f"{label!r} did not anchor the value"
    )

@pytest.mark.parametrize('label', recognizers.LABEL_SYNONYMS['HEALTH_PLAN_ID'])
def test_every_health_plan_label_synonym_is_detected(label):
    text = f"Coverage.\n{label.title()}: 1EG4TE5MK73\nEffective January."
    assert '1EG4TE5MK73' in redactable(text, 'HEALTH_PLAN_ID')

@pytest.mark.parametrize('label', recognizers.LABEL_SYNONYMS['ACCOUNT_NUMBER'])
def test_every_account_label_synonym_is_detected(label):
    text = f"Billing.\n{label.title()}: ACCT-77120934\nBalance due."
    assert 'ACCT-77120934' in redactable(text, 'ACCOUNT_NUMBER')

def test_a_bare_identifier_with_no_label_is_not_redacted():
    """The counterweight to the tests above. These recognizers have no format
    to match, so without a label they must stay below the floor -- otherwise
    every alphanumeric token in the document would be replaced."""
    text = "The suture pack contained 4417392 units of material."
    assert redactable(text, 'MEDICAL_RECORD_NUMBER') == []

def test_the_label_itself_survives_redaction():
    """Only the value is replaced. Removing 'Medical Record Number:' would
    destroy the structure of the record without hiding anything."""
    text = "Medical Record Number: 4417392\n"
    result = strip_pii(text, "note.pdf", output_mode='rag')
    assert result.text.startswith("Medical Record Number: ")
    assert "4417392" not in result.text


def test_unrecognised_label_with_a_digit_run_is_not_silent():
    """Plan Q2: a label-like token followed by a value that no synonym list
    claims must still surface. Silence is the failure mode being removed.
    U7 turns this signal into a quarantine; U8 records it."""
    text = "Regional Oncology Consortium Code: RX-99417-B\n"
    signalled = [matched for kind, matched, _ in detect(text)
                 if kind == 'UNRESOLVED_IDENTIFIER']
    assert signalled, f"no signal raised, detections were: {detect(text)}"


# --- structural identifiers (R7) ---------------------------------------------

@pytest.mark.parametrize('entity,text,value', [
    ('STREET_ADDRESS', "Address: 88 Larkspur Lane\n", "88 Larkspur Lane"),
    ('STREET_ADDRESS', "He lives at 123 Main Street, Chicago.", "123 Main Street"),
    ('US_ZIP', "Riverton, IL 60655\n", "60655"),
    ('US_ZIP', "ZIP: 60655\n", "60655"),
    ('VEHICLE_ID', "Vehicle Identification Number: 1HGCM82633A004352\n",
     "1HGCM82633A004352"),
    ('VEHICLE_ID', "License Plate: IL PVT-4417\n", "PVT-4417"),
    ('DEVICE_SERIAL', "Device UDI: (01)00819320041234(17)270630(10)LOT4471\n",
     "(01)00819320041234(17)270630(10)LOT4471"),
    ('DEVICE_SERIAL', "Implanted Device Serial Number: SN-4471-XQ9920\n",
     "SN-4471-XQ9920"),
    ('URL', "Patient Portal: https://portal.example-clinic.org/patients/wferris91\n",
     "https://portal.example-clinic.org/patients/wferris91"),
])
def test_structural_identifier_is_detected(entity, text, value):
    matches = redactable(text, entity)
    assert any(value in m for m in matches), (
        f"{entity} did not cover {value!r}; got {matches} from {detect(text)}"
    )


# --- ages (R8, KTD8) ----------------------------------------------------------

def test_age_over_89_is_aggregated_not_date_shifted():
    text = "Mr. Vance is a 91-year-old man admitted for surgery."
    result = strip_pii(text, "note.pdf", output_mode='finetune')
    assert "90 or older" in result.text
    assert "91-year-old" not in result.text
    assert not re.search(r'\d{4}-\d{2}-\d{2}', result.text), (
        f"the age was routed through date handling: {result.text!r}"
    )

def test_labelled_age_over_89_is_aggregated():
    result = strip_pii("The patient is age 94.", "note.pdf", output_mode='finetune')
    assert "90 or older" in result.text
    assert "94" not in result.text

@pytest.mark.parametrize('age', ['45', '89'])
def test_age_89_and_under_is_preserved(age):
    """Safe Harbor aggregates ages over 89 only. Replacing an ordinary age
    would destroy clinical meaning for no privacy gain."""
    text = f"The patient is a {age}-year-old woman."
    result = strip_pii(text, "note.pdf", output_mode='finetune')
    assert f"{age}-year-old" in result.text
    assert "90 or older" not in result.text


# --- the U4 invariant, asserted here too --------------------------------------

def test_new_recognizers_preserve_every_line(lines_preserved):
    """Asserted in this unit so a new recognizer's span defect surfaces now
    rather than five units later."""
    from pathlib import Path
    corpus = Path(__file__).parent / 'fixtures' / 'corpus'
    for document in sorted(corpus.glob('*.txt')):
        source = document.read_text()
        for mode in ('finetune', 'rag'):
            result = strip_pii(source, document.name, output_mode=mode)
            lines_preserved(source, result.text)
