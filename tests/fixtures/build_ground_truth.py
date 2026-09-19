"""Regenerate tests/fixtures/corpus/ground_truth.json from the corpus documents.

Run from the repository root:  python3.12 tests/fixtures/build_ground_truth.py

The declarations below are the authority for what each corpus document is
known to contain; this script only resolves them to offsets so the manifest
and the fixture text cannot drift apart silently. test_scrub_quality.py
asserts every recorded offset still slices out its recorded value.

Every value is invented, with three deliberate exceptions marked `published
reference value` -- the NPI, MBI and DEA examples published by CMS in their
own format documentation. Nothing here is derived from a real patient record.
"""
import json
from pathlib import Path

CORPUS = Path(__file__).parent / 'corpus'

# (category, Safe Harbor paragraph, value)
DECLARATIONS = {
    'discharge_001.txt': [
        ('name',            'A', 'Harold Vance'),
        ('name',            'A', 'Dorothy Kimball'),
        ('mrn',             'H', '4417392'),
        ('date',            'C', '03/14/1951'),
        ('date',            'C', '08/02/2025'),
        ('date',            'C', '08/05/2025'),
        ('ssn',             'G', '412-55-9083'),
        ('street_address',  'B', '88 Larkspur Lane'),
        ('city',            'B', 'Riverton'),
        ('zip',             'B', '60655'),
        ('phone',           'D', '(312) 555-0147'),
        ('fax',             'E', '(312) 555-0148'),
        ('email',           'F', 'hvance@example.net'),
        ('age_over_89',     'C', '91-year-old'),
        ('account_number',  'J', 'ACCT-77120934'),
    ],
    'clinic_note_002.txt': [
        ('name',            'A', 'Wanda Ferris'),
        ('name',            'A', 'Marcus Oyelaran'),
        ('health_plan_id',  'I', '1EG4TE5MK73'),   # published reference value (CMS MBI)
        ('health_plan_id',  'I', 'GRP-4482'),
        ('account_number',  'J', '88-4410-22'),
        ('license_number',  'K', '1234567893'),    # published reference value (CMS NPI)
        ('license_number',  'K', 'AB1234563'),     # published reference value (CMS DEA)
        ('license_number',  'K', 'IL-036-114829'),
        ('email',           'F', 'wferris@example.org'),
        ('phone',           'D', '773-555-0166'),
        ('url',             'N', 'https://portal.example-clinic.org/patients/wferris91'),
        ('ip_address',      'O', '203.0.113.47'),
        ('date',            'C', '09/11/2025'),
        ('date',            'C', '10/09/2025'),
    ],
    # Identifiers embedded in running prose rather than labelled fields, plus
    # alternate date formats and an age over 89.
    'er_note_004.txt': [
        ('name',            'A', 'Beatrice Okonkwo'),
        ('name',            'A', 'Alan Petrosyan'),
        ('age_over_89',     'C', '93-year-old'),
        ('mrn',             'H', '5520118'),
        ('health_plan_id',  'I', '2FT6WE9NQ85'),
        ('health_plan_id',  'I', 'POL-7781204'),
        ('account_number',  'J', 'GA-99413'),
        ('license_number',  'K', 'BP9876547'),
        ('street_address',  'B', '412 Sycamore Terrace'),
        ('city',            'B', 'Riverton'),
        ('zip',             'B', '60658'),
        ('phone',           'D', '312-555-0193'),
        ('fax',             'E', '(312) 555-0194'),
        ('email',           'F', 'bokonkwo@example.com'),
        ('date',            'C', 'Mar. 4, 2025'),
        ('date',            'C', '2025-03-06'),
    ],
    'radiology_005.txt': [
        ('name',            'A', 'Terrence Whitlock'),
        ('name',            'A', 'Priya Ramanathan'),
        ('mrn',             'H', 'MRN-6640933'),
        ('other_unique_id', 'R', 'ACC-2025-88214'),
        ('date',            'C', '15 April 2025'),
        ('license_number',  'K', '1740467307'),
        ('device_serial',   'M', 'EQ-77120-B4'),
        ('device_serial',   'M', '(01)00312890044178(11)250101(21)SCN9931'),
        ('ip_address',      'O', '198.51.100.22'),
        ('url',             'N',
         'https://imaging.example-riverton.org/results/twhitlock66'),
        ('email',           'F', 'pramanathan@example.org'),
        ('phone',           'D', '773-555-0121'),
    ],
    'device_report_003.txt': [
        ('name',            'A', 'Cecil Broadwater'),
        ('mrn',             'H', 'MRN-8830271'),
        ('date',            'C', '06/18/2025'),
        ('device_serial',   'M', 'SN-4471-XQ9920'),
        ('device_serial',   'M', '(01)00819320041234(17)270630(10)LOT4471'),
        ('device_serial',   'M', 'ASSET-220417'),
        ('vehicle_id',      'L', 'IL PVT-4417'),
        ('vehicle_id',      'L', '1HGCM82633A004352'),
        ('other_unique_id', 'R', 'RVT-2025-0431'),
    ],
}


def occurrences(text: str, value: str) -> list:
    spans, cursor = [], text.find(value)
    while cursor != -1:
        spans.append([cursor, cursor + len(value)])
        cursor = text.find(value, cursor + 1)
    return spans


def build() -> dict:
    manifest = {'documents': {}}
    for filename, declarations in DECLARATIONS.items():
        text = (CORPUS / filename).read_text()
        entries = []
        for category, paragraph, value in declarations:
            spans = occurrences(text, value)
            if not spans:
                raise SystemExit(
                    f"{filename}: declared {category} value {value!r} is not in the document"
                )
            entries.append({
                'category': category,
                'safe_harbor': paragraph,
                'value': value,
                'spans': spans,
            })
        manifest['documents'][filename] = entries
    return manifest


if __name__ == '__main__':
    out = CORPUS / 'ground_truth.json'
    out.write_text(json.dumps(build(), indent=2) + '\n')
    total = sum(len(v) for v in build()['documents'].values())
    print(f"wrote {out} — {total} identifiers across {len(DECLARATIONS)} documents")
