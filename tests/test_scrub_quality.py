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


def decoded_record_text(output_dir: Path) -> str:
    """Every output record's decoded text, concatenated per file.

    Not a line-oriented grep: records are one per line, so an identifier
    straddling a chunk split has half on one line and half on the next and
    matches neither. Concatenating the decoded text of each file's records
    catches it.
    """
    blobs = []
    for path in sorted(output_dir.rglob('*.jsonl')):
        parts = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                parts.append(json.loads(line).get('text', ''))
            except json.JSONDecodeError:
                parts.append(line)
        blobs.append(''.join(parts))
    return '\n'.join(blobs)


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
        # Measured from the scrubbed output, not from the detections, and in
        # *both* modes.
        #
        # "Covered by a detection" is not the same as "removed": a chart number
        # and a ZIP were both detected as DATE_TIME at 0.85 and then written
        # straight back out, because the date replacement returns its input
        # unchanged when the value will not parse as a date. Scoring on
        # detections reported 1.00 for both.
        #
        # And scoring only placeholder mode missed it again, because that mode
        # replaces through the library's own operator while the deliverable for
        # a private document is the Faker-mode record. An identifier counts as
        # protected only when it is gone from both.
        scrubbed = {
            mode: strip_pii(text, filename, output_mode=mode).text
            for mode in ('rag', 'finetune')
        }

        for entry in entries:
            category = entry['category']
            total[category] += 1
            leaked_in = [mode for mode, out in scrubbed.items()
                         if entry['value'] in out]
            if not leaked_in:
                caught[category] += 1
            else:
                survivors[category].append(
                    f"{filename}:{entry['value']} ({'+'.join(leaked_in)})"
                )

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
    that cannot register a regression is not measuring anything.

    URL rather than SSN because URL is the only recognizer covering that
    category. An SSN is covered twice over -- see the test below -- so removing
    one of the two proves nothing about the scorer.
    """
    assert baseline['recall'].get('url', 0) == 1.0, (
        "precondition: URL must be detected at baseline for this test to mean anything"
    )
    monkeypatch.setattr(
        pii_module, 'ENTITY_TYPES',
        [e for e in pii_module.ENTITY_TYPES if e != 'URL'],
    )
    degraded = score_corpus()
    assert degraded['recall']['url'] < baseline['recall']['url'], (
        "removing the URL recognizer did not reduce URL recall -- the scorer is "
        "not bound to the tool's actual detections"
    )

def test_an_ssn_is_covered_by_more_than_one_recognizer(monkeypatch):
    """Defence in depth, and only true since the floor dropped to 0.30: with
    the dedicated SSN recognizer removed, the labelled value is still caught by
    UNRESOLVED_IDENTIFIER at 0.40. Pinned because it is the difference between
    one recognizer regressing and an identifier being disclosed."""
    monkeypatch.setattr(
        pii_module, 'ENTITY_TYPES',
        [e for e in pii_module.ENTITY_TYPES if e != 'US_SSN'],
    )
    result = strip_pii("Social Security Number: 412-55-9083\n", "note.txt",
                       output_mode='rag')
    assert "412-55-9083" not in result.text

# --- U10: the floors ----------------------------------------------------------

# Missing one of these is a disclosure, not a degraded metric.
HIGH_HARM = ('ssn', 'mrn', 'account_number', 'health_plan_id')

# Every category has to clear this. The counts per category are small, so in
# practice it means "no survivors"; it is written as a floor because U10's
# corpus is a living fixture and will grow.
RECALL_FLOOR = 0.95

# KTD9's guardrail. The evidence for lowering the detection floor is that
# downstream analysis is stable across moderate-to-high precision and collapses
# only once precision does, so this is set to catch a collapse rather than to
# pin the current number.
PRECISION_FLOOR = 0.80


def test_no_high_harm_identifier_survives(baseline):
    """Regardless of aggregate score. An SSN, a medical record number, an
    account number or a member number left in the text is the disclosure this
    whole plan exists to prevent."""
    for category in HIGH_HARM:
        assert category in baseline['recall'], f"{category} is not in the corpus"
        assert baseline['recall'][category] == 1.0, (
            f"{category} survivors: {baseline['survivors'].get(category)}"
        )

def test_every_category_meets_its_recall_floor(baseline):
    below = {
        category: (value, baseline['survivors'].get(category))
        for category, value in baseline['recall'].items()
        if value < RECALL_FLOOR
    }
    assert not below, f"below the {RECALL_FLOOR} recall floor: {below}"

def test_precision_meets_its_floor(baseline):
    """The counterweight to a 0.30 detection floor: over-redaction is the
    cheaper error, but only while precision holds."""
    assert baseline['precision'] >= PRECISION_FLOOR, (
        f"precision {baseline['precision']:.2f} is below {PRECISION_FLOOR}"
    )


def test_the_no_leak_search_concatenates_records_rather_than_grepping_lines(tmp_path):
    """Output records are one per line, so an identifier straddling a chunk
    split has half on each line and a line-oriented grep matches neither."""
    dataset = tmp_path / 'dataset.jsonl'
    dataset.write_text(
        json.dumps({'text': 'Social Security Number: 412-55-'}) + '\n'
        + json.dumps({'text': '9083 was recorded on intake.'}) + '\n'
    )

    line_oriented = [l for l in dataset.read_text().splitlines()
                     if '412-55-9083' in l]
    assert line_oriented == [], "the fixture does not actually straddle a split"

    assert '412-55-9083' in decoded_record_text(tmp_path), (
        "the concatenating search missed a straddling identifier"
    )


def test_every_line_is_preserved_corpus_wide(lines_preserved):
    for filename in sorted(GROUND_TRUTH):
        source = (CORPUS / filename).read_text()
        for mode in ('finetune', 'rag'):
            lines_preserved(source, strip_pii(source, filename, mode).text)


def test_an_identifier_across_the_analysis_boundary_is_detected(monkeypatch):
    """Over 900,000 characters the text is chunked. A straddling identifier is
    a silent false negative, not an error, so it needs its own check."""
    monkeypatch.setattr(pii_module, 'MAX_PRESIDIO_CHARS', 600)
    monkeypatch.setattr(pii_module, 'CHUNK_OVERLAP_CHARS', 150)
    filler = "The patient was seen in the outpatient clinic today. "
    text = (filler * 40)[:595] + "412-55-9083" + " " + (filler * 5)
    assert text.index("412-55-9083") < 600 < text.index("412-55-9083") + 11

    scrubbed = strip_pii(text, "long.txt", output_mode='rag').text
    assert "412-55-9083" not in scrubbed
    assert scrubbed.count("[US_SSN]") == 1


def test_no_leak_check_is_bound_to_ground_truth_not_to_the_tool(baseline):
    """The survivors list must come from the corpus manifest. Checking the
    tool's output against the tool's own findings would report zero leaks even
    if a whole recognizer silently stopped firing."""
    declared = {e['value'] for entries in GROUND_TRUTH.values() for e in entries}
    for listed in baseline['survivors'].values():
        for item in listed:
            assert item.split(':', 1)[1] in declared
