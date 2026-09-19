"""Per-category scrub quality against a corpus with known contents.

U13 builds the measuring instrument and records a baseline. It deliberately
asserts no recall or precision floors -- U10 adds those once the recognizers
exist. What this file does assert is that the instrument itself works: that the
ground truth matches the fixtures, and that recall demonstrably falls when a
recognizer is removed.

Scoring is entity-level, not token-level: an identifier counts as protected
only when every character of every one of its mentions is covered by a
detection the tool would actually act on. Partial coverage is a leak, not a
partial success.
"""
import json
from collections import defaultdict
from pathlib import Path

import pytest

import pii as pii_module
from pii import strip_pii

CORPUS = Path(__file__).parent / 'fixtures' / 'corpus'
GROUND_TRUTH = json.loads((CORPUS / 'ground_truth.json').read_text())['documents']

# The 16 Safe Harbor paragraphs that can appear in extracted text: the eighteen
# at 45 CFR 164.514(b)(2) less (P) biometric identifiers and (Q) full-face
# photographs.
TEXT_TESTABLE_PARAGRAPHS = set("ABCDEFGHIJKLMNOR")


def detections_for(text: str) -> list:
    """The spans the tool would actually redact: clamped, and over the floor."""
    analyzer, _ = pii_module._get_engines()
    results = pii_module._clamp_over_long_spans(
        text, pii_module._analyze_chunked(analyzer, text)
    )
    return [r for r in results if r.score >= pii_module.LOW_CONFIDENCE]


def _fully_covered(span, detections) -> bool:
    start, end = span
    covered = bytearray(end - start)
    for d in detections:
        for i in range(max(start, d.start), min(end, d.end)):
            covered[i - start] = 1
    return all(covered)


def _overlaps_any(detection, spans) -> bool:
    return any(detection.start < e and s < detection.end for s, e in spans)


def score_corpus() -> dict:
    """Per-category recall, plus precision as the over-redaction counterweight."""
    caught = defaultdict(int)
    total = defaultdict(int)
    survivors = defaultdict(list)
    true_positives = 0
    all_detections = 0
    detections_by_type = defaultdict(lambda: [0, 0])  # [true positive, total]

    for filename, entries in GROUND_TRUTH.items():
        text = (CORPUS / filename).read_text()
        detections = detections_for(text)
        truth_spans = [tuple(s) for e in entries for s in e['spans']]

        for entry in entries:
            category = entry['category']
            total[category] += 1
            if all(_fully_covered(span, detections) for span in entry['spans']):
                caught[category] += 1
            else:
                survivors[category].append(f"{filename}:{entry['value']}")

        for d in detections:
            all_detections += 1
            hit = _overlaps_any(d, truth_spans)
            true_positives += hit
            detections_by_type[d.entity_type][1] += 1
            detections_by_type[d.entity_type][0] += hit

    return {
        'recall': {c: caught[c] / total[c] for c in total},
        'counts': {c: (caught[c], total[c]) for c in total},
        'survivors': dict(survivors),
        'precision': (true_positives / all_detections) if all_detections else 0.0,
        'precision_by_type': {
            t: (tp / n) for t, (tp, n) in detections_by_type.items() if n
        },
    }


@pytest.fixture(scope='module')
def baseline():
    return score_corpus()


def format_table(report: dict) -> str:
    lines = ["", "per-category recall (entity-level; every mention must be caught)", ""]
    lines.append(f"  {'category':18s} {'recall':>7s}  {'caught':>10s}   survivors")
    for category in sorted(report['recall']):
        caught, total = report['counts'][category]
        survivors = ', '.join(report['survivors'].get(category, [])) or '-'
        lines.append(
            f"  {category:18s} {report['recall'][category]:7.2f}  "
            f"{caught:>4d}/{total:<5d}   {survivors}"
        )
    lines.append("")
    lines.append(f"  overall precision (detections overlapping a known identifier): "
                 f"{report['precision']:.2f}")
    for entity_type in sorted(report['precision_by_type']):
        lines.append(f"    {entity_type:22s} {report['precision_by_type'][entity_type]:.2f}")
    return '\n'.join(lines) + '\n'


# --- the instrument itself ---------------------------------------------------

def test_every_recorded_offset_still_matches_its_value():
    """Guards the manifest against drifting away from the fixture text."""
    for filename, entries in GROUND_TRUTH.items():
        text = (CORPUS / filename).read_text()
        for entry in entries:
            for start, end in entry['spans']:
                assert text[start:end] == entry['value'], (
                    f"{filename}: offset {start}:{end} no longer holds "
                    f"{entry['value']!r} -- re-run tests/fixtures/build_ground_truth.py"
                )

def test_corpus_covers_every_text_testable_safe_harbor_paragraph():
    present = {e['safe_harbor'] for entries in GROUND_TRUTH.values() for e in entries}
    assert present == TEXT_TESTABLE_PARAGRAPHS, (
        f"missing: {sorted(TEXT_TESTABLE_PARAGRAPHS - present)}, "
        f"unexpected: {sorted(present - TEXT_TESTABLE_PARAGRAPHS)}"
    )

def test_scorer_reports_a_number_for_every_category(baseline, capsys):
    """The baseline, whatever it is. U13 establishes it; U10 asserts floors."""
    with capsys.disabled():
        print(format_table(baseline))
    assert baseline['recall'], "scorer produced no categories at all"
    for category, value in baseline['recall'].items():
        assert 0.0 <= value <= 1.0, f"{category} recall out of range: {value}"

def test_removing_a_recognizer_drops_that_categorys_recall(baseline, monkeypatch):
    """Proves the instrument can fail before anything depends on it. A scorer
    that cannot register a regression is not measuring anything."""
    assert baseline['recall'].get('ssn', 0) > 0, (
        "precondition: SSN must be detected at baseline for this test to mean anything"
    )
    monkeypatch.setattr(
        pii_module, 'ENTITY_TYPES',
        [e for e in pii_module.ENTITY_TYPES if e != 'US_SSN'],
    )
    degraded = score_corpus()
    assert degraded['recall']['ssn'] < baseline['recall']['ssn'], (
        "removing the SSN recognizer did not reduce SSN recall -- the scorer is "
        "not bound to the tool's actual detections"
    )

def test_no_leak_check_is_bound_to_ground_truth_not_to_the_tool(baseline):
    """The survivors list must come from the corpus manifest. Checking the
    tool's output against the tool's own findings would report zero leaks even
    if a whole recognizer silently stopped firing."""
    declared = {e['value'] for entries in GROUND_TRUTH.values() for e in entries}
    for listed in baseline['survivors'].values():
        for item in listed:
            assert item.split(':', 1)[1] in declared
