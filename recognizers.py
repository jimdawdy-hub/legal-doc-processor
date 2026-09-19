"""Recognizer definitions for the identifiers presidio does not ship.

Split out of pii.py so that module keeps engine construction, operators,
thresholds and seeding, while recognizer definitions live together.

Two kinds live here.

**Label-anchored** (KTD6). Medical record numbers, health plan member numbers
and account numbers have no national format and no checksum, so there is
nothing to pattern-match except the label that introduces them. Each is a
broad identifier-shaped value pattern scored deliberately *below* the
redaction floor, plus a context word list. Presidio's context enhancer lifts a
match to 0.55 when one of those words appears just before it, so a labelled
value is redacted and a bare alphanumeric token elsewhere in the document is
not. Measured: '4417392' alone scores 0.20, the same digits after 'Medical
Record Number:' score 0.55.

The matched span is the value only. The label stays in the text, because
removing it would destroy the structure of the record without hiding anything.

**Structural.** ZIP, street address, VIN and the age generalisation have a
shape of their own and are scored on that shape alone.
"""
import re

from presidio_analyzer import LocalRecognizer, Pattern, PatternRecognizer, RecognizerResult

# A label immediately followed by an identifier-shaped value is strong
# evidence, so this sits above DATE_TIME (0.85) -- otherwise a medical record
# number is redacted as a date, which protects it but replaces it with the
# wrong kind of value (R4). It stays below the SSN pattern's 0.90.
LABEL_ANCHORED_SCORE = 0.88

# Scored below every redaction floor in the pipeline (0.50 today, 0.30 after
# the floor drops) so an unlabelled identifier-shaped token stays visible as a
# signal without being redacted. Presidio's context enhancer adds 0.35.
UNLABELLED_SCORE = 0.20

# An identifier-shaped token: four or more characters, starting alphanumeric,
# containing at least one digit. Covers '4417392', 'MRN-8830271', 'GRP-4482',
# 'ACCT-77120934', '88-4410-22', 'SN-4471-XQ9920', '1EG4TE5MK73'.
ID_VALUE_INNER = r'(?=[A-Za-z0-9\-/]*\d)[A-Za-z0-9][A-Za-z0-9\-/]{3,}\b'
ID_VALUE = rf'\b{ID_VALUE_INNER}'

# Documented synonym lists (plan Q2). Extend these against real documents --
# the literature is consistent that institution-specific labels are the
# category automated systems under-detect most.
LABEL_SYNONYMS = {
    'MEDICAL_RECORD_NUMBER': [
        'medical record', 'medical record number', 'mrn', 'mr number',
        'chart', 'chart number', 'patient number', 'patient id',
        'patient identifier', 'health record number', 'unit number',
    ],
    'HEALTH_PLAN_ID': [
        'member', 'member number', 'member id', 'beneficiary',
        'beneficiary number', 'health plan', 'plan id', 'policy',
        'policy number', 'group', 'group number', 'subscriber',
        'subscriber id', 'medicare', 'medicaid', 'insurance id', 'mbi',
    ],
    'ACCOUNT_NUMBER': [
        'account', 'account number', 'acct', 'billing', 'billing account',
        'invoice', 'invoice number', 'guarantor account', 'statement',
    ],
    'DEVICE_SERIAL': [
        'serial', 'serial number', 'device', 'device id', 'udi',
        'asset', 'asset tag', 'implant', 'lot', 'catalog', 'model',
        'equipment', 'pump',
    ],
    'OTHER_IDENTIFIER': [
        'study', 'participant', 'study id', 'participant code', 'case number',
        'specimen', 'accession', 'requisition', 'visit number', 'encounter',
        'authorization', 'certificate', 'reference number',
        # Safe Harbor (K), certificate and licence numbers issued by a body
        # with no national format of its own -- a state medical licence, a
        # facility permit. The checksum-validated national ones (NPI, DEA,
        # MBI) are recognised by presidio's own validators, not here.
        'license', 'licence', 'license number', 'licence number',
        'registration', 'registration number', 'provider number', 'permit',
    ],
}

# A label-like token followed by a value that no specific recognizer above
# claims. Scored above the post-U7 floor but below CONTEXT_BOOSTED_SCORE, so a
# recognised type always wins the overlap and this one only surfaces what the
# synonym lists missed. Plan Q2: silence is the failure mode being removed.
UNRESOLVED_SCORE = 0.40
UNRESOLVED_LABEL = (
    r'(?:[A-Za-z][A-Za-z.\-/]{2,20}\s+){0,3}'
    r'(?:number|no\.?|id|code|identifier|licen[cs]e|certificate)'
)

STREET_SUFFIXES = (
    'street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|court|ct|'
    'way|place|pl|terrace|ter|circle|cir|parkway|pkwy|highway|hwy|trail|trl|'
    'square|sq|suite|ste|apartment|apt|unit'
)


class LabelAnchoredRecognizer(LocalRecognizer):
    """Match `<label> : <value>` and return a result covering the value only.

    Presidio's own context enhancer is not usable for this. It compares single
    word lemmas in a window around a match, so a multi-word label like
    'medical record number' never matches at all, while a single word like
    'patient' or 'record' matches so much of a clinical note that it would
    boost every identifier-shaped token in the document. Measured: with the
    context approach, eight of eleven medical-record label synonyms failed to
    anchor their value, and the corpus only scored well because DATE_TIME
    happened to cover the digits.

    A PatternRecognizer cannot express this either, because it reports
    `match.span()` and ignores capture groups, so the label would be redacted
    along with the value. Python has no variable-length lookbehind, so the
    span is narrowed here instead.
    """

    def __init__(self, supported_entity: str, labels: list = None,
                 label_regex: str = None, score: float = LABEL_ANCHORED_SCORE,
                 name: str = None):
        super().__init__(
            supported_entities=[supported_entity],
            name=name or f"{supported_entity.title().replace('_', '')}Recognizer",
        )
        self.entity = supported_entity
        self.score = score
        if label_regex is None:
            # Longest first, so 'medical record number' wins over 'medical record'.
            label_regex = '|'.join(
                re.escape(label) for label in sorted(labels, key=len, reverse=True)
            )
        self.pattern = re.compile(
            rf'\b(?:{label_regex})\b'           # the label
            rf'\s*(?:number|no\.?|id|code|#)?'  # an optional trailing noun
            rf'(?:\s*[:#\-]\s*|\s+)'            # separator, or just whitespace
            rf'(?P<value>{ID_VALUE_INNER})',
            re.IGNORECASE,
        )

    def load(self) -> None:
        return None

    def analyze(self, text: str, entities: list, nlp_artifacts=None) -> list:
        if self.entity not in entities:
            return []
        return [
            RecognizerResult(
                entity_type=self.entity,
                start=match.start('value'),
                end=match.end('value'),
                score=self.score,
            )
            for match in self.pattern.finditer(text)
        ]


def build_recognizers() -> list:
    """Every recognizer this project adds to presidio's default registry."""
    recognizers = [
        LabelAnchoredRecognizer(entity, synonyms)
        for entity, synonyms in LABEL_SYNONYMS.items()
    ]

    recognizers.append(LabelAnchoredRecognizer(
        "UNRESOLVED_IDENTIFIER",
        label_regex=UNRESOLVED_LABEL,
        score=UNRESOLVED_SCORE,
        name="UnresolvedIdentifierRecognizer",
    ))

    # Supplements presidio's built-in US_SSN with an explicit high-confidence
    # pattern so NNN-NN-NNNN is always caught regardless of its validation
    # scoring. Moved here from pii.py's engine setup.
    recognizers.append(PatternRecognizer(
        supported_entity="US_SSN",
        name="SsnPatternRecognizer",
        patterns=[Pattern("ssn_dashed", r'\b\d{3}-\d{2}-\d{4}\b', 0.90)],
        context=["ssn", "social security", "social security number"],
    ))

    # Safe Harbor (B): geography below state level. The city is caught by
    # spaCy's LOCATION; the street line and the ZIP were not.
    recognizers.append(PatternRecognizer(
        supported_entity="STREET_ADDRESS",
        name="StreetAddressRecognizer",
        patterns=[Pattern(
            "street_line",
            rf'\b\d{{1,6}}\s+(?:[A-Z][A-Za-z.\'-]+\s+){{1,3}}(?:{STREET_SUFFIXES})\b\.?',
            0.70,
        )],
        global_regex_flags=re.IGNORECASE | re.MULTILINE,
    ))
    recognizers.append(PatternRecognizer(
        supported_entity="US_ZIP",
        name="UsZipRecognizer",
        patterns=[
            # A ZIP immediately after a two-letter state is unambiguous.
            Pattern("state_zip", r'(?<=\b[A-Z]{2}\s)\d{5}(?:-\d{4})?\b', 0.65),
            # Otherwise a bare five-digit run needs a label to be redacted.
            Pattern("bare_zip", r'\b\d{5}(?:-\d{4})?\b', UNLABELLED_SCORE),
        ],
        context=["zip", "zip code", "postal", "postal code", "address", "city",
                 "state", "mailing"],
    ))

    # Safe Harbor (L): vehicle identifiers including licence plates.
    recognizers.append(PatternRecognizer(
        supported_entity="VEHICLE_ID",
        name="VehicleIdRecognizer",
        patterns=[
            # A VIN is 17 characters and never contains I, O or Q.
            Pattern("vin", r'\b[A-HJ-NPR-Z0-9]{17}\b', 0.75),
            Pattern("plate_value", r'\b(?:[A-Z]{2}\s)?[A-Z]{2,3}[- ]?\d{3,4}\b',
                    UNLABELLED_SCORE),
        ],
        context=["license plate", "licence plate", "plate", "vehicle", "vin",
                 "registration", "tag"],
    ))

    # Safe Harbor (M): a UDI carries its own bracketed application identifiers.
    recognizers.append(PatternRecognizer(
        supported_entity="DEVICE_SERIAL",
        name="DeviceUdiRecognizer",
        patterns=[Pattern("udi", r'\(\d{2}\)[A-Za-z0-9()]{8,}', 0.80)],
    ))

    # Safe Harbor (C): ages over 89 are aggregated rather than preserved, and
    # must not be routed through date handling -- '91-year-old' was detected as
    # DATE_TIME at 0.85 and date-shifted, which invents a birthday.
    recognizers.append(PatternRecognizer(
        supported_entity="AGE_OVER_89",
        name="AgeOver89Recognizer",
        # Both patterns deliberately span the label as well as the number.
        # DATE_TIME matches 'age 94' at 0.85, so an age span covering only the
        # digits loses the overlap to it and the age is date-shifted instead of
        # aggregated -- measured: 'The patient is age 94.' became 'The patient
        # is 1996-03-11.'
        patterns=[
            Pattern("age_year_old",
                    r'\b(?:9\d|1\d\d)[\s-]*(?:year|yr)s?[\s-]*old\b', 0.95),
            Pattern("age_labelled",
                    r'\bage[ds]?\s*:?\s*(?:of\s+)?(?:9\d|1\d\d)\b', 0.95),
        ],
        global_regex_flags=re.IGNORECASE | re.MULTILINE,
    ))

    return recognizers
