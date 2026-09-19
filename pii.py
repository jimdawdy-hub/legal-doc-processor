import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from utils import sha256_file

from faker import Faker
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.chunkers import CharacterBasedTextChunker

from recognizers import build_recognizers
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import ConflictResolutionStrategy, OperatorConfig

try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_AVAILABLE = True
except ImportError:
    DATEUTIL_AVAILABLE = False


@dataclass
class PIIResult:
    text: str
    substitutions: int
    review_flags: List[dict] = field(default_factory=list)


HIGH_CONFIDENCE = 0.85
MAX_PRESIDIO_CHARS = 900_000  # spaCy's default limit is 1M; stay safely under it
# Overlap between analysis chunks. Must exceed the longest identifier format in
# scope, or an identifier longer than the overlap can still straddle a cut.
CHUNK_OVERLAP_CHARS = 512
LOW_CONFIDENCE = 0.50

ENTITY_TYPES = [
    # Present since the first version.
    "PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION",
    "US_SSN", "DATE_TIME", "US_BANK_NUMBER", "CREDIT_CARD",
    "US_PASSPORT", "US_DRIVER_LICENSE", "IP_ADDRESS", "MEDICAL_LICENSE",
    # Safe Harbor categories the detector used to ignore (U5).
    "URL", "STREET_ADDRESS", "US_ZIP", "AGE_OVER_89",
    "MEDICAL_RECORD_NUMBER", "HEALTH_PLAN_ID", "ACCOUNT_NUMBER",
    "DEVICE_SERIAL", "VEHICLE_ID", "OTHER_IDENTIFIER",
    # A labelled value no specific recognizer above claimed. Never silent:
    # this is the signal that a label was understood and its value was not.
    "UNRESOLVED_IDENTIFIER",
]

_analyzer: Optional[AnalyzerEngine] = None
_anonymizer: Optional[AnonymizerEngine] = None

# Month name tables for format preservation
_MONTH_FULL  = ['January','February','March','April','May','June',
                'July','August','September','October','November','December']
_MONTH_SHORT = ['Jan','Feb','Mar','Apr','May','Jun',
                'Jul','Aug','Sep','Oct','Nov','Dec']

# Common date format patterns — (compiled_regex, formatter_fn)
# Checked in order; first match wins.
def _fmt_month_d_yyyy(dt, _orig):
    return f"{_MONTH_FULL[dt.month-1]} {dt.day}, {dt.year}"

def _fmt_mon_d_yyyy(dt, orig):
    # Preserve abbreviation style (with or without period)
    abbr = _MONTH_SHORT[dt.month-1]
    return f"{abbr}{'.' if '.' in orig else ''} {dt.day}, {dt.year}"

def _fmt_slash_mdy(dt, orig):
    # Preserve zero-padding of original
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', orig)
    if m:
        month_pad = len(m.group(1)) == 2
        day_pad   = len(m.group(2)) == 2
        mo = f"{dt.month:02d}" if month_pad else str(dt.month)
        da = f"{dt.day:02d}"   if day_pad   else str(dt.day)
        return f"{mo}/{da}/{dt.year}"
    return f"{dt.month}/{dt.day}/{dt.year}"

def _fmt_dash_mdy(dt, orig):
    return f"{dt.month:02d}-{dt.day:02d}-{dt.year}"

def _fmt_iso(dt, _orig):
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"

def _fmt_d_month_yyyy(dt, _orig):
    return f"{dt.day} {_MONTH_FULL[dt.month-1]} {dt.year}"

def _fmt_month_yyyy(dt, _orig):
    return f"{_MONTH_FULL[dt.month-1]} {dt.year}"

def _fmt_mon_yyyy(dt, _orig):
    return f"{_MONTH_SHORT[dt.month-1]} {dt.year}"

def _fmt_year_only(dt, _orig):
    # R4: a bare year is a year. Returning a full date for "2019" invented a
    # month and a day that were never in the document.
    return str(dt.year)

_DATE_FORMAT_PATTERNS = [
    # Full month name + day + year:  "March 15, 2024"
    (re.compile(r'\b(?:January|February|March|April|May|June|July|August|'
                r'September|October|November|December)\s+\d{1,2},?\s+\d{4}\b', re.I),
     _fmt_month_d_yyyy),
    # Short month + day + year:  "Mar. 15, 2024" or "Mar 15, 2024"
    (re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4}\b', re.I),
     _fmt_mon_d_yyyy),
    # ISO: "2024-03-15"
    (re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),
     _fmt_iso),
    # Slash MM/DD/YYYY: "3/15/2024" or "03/15/2024"
    (re.compile(r'\b\d{1,2}/\d{1,2}/\d{4}\b'),
     _fmt_slash_mdy),
    # Dash MM-DD-YYYY: "03-15-2024"
    (re.compile(r'\b\d{2}-\d{2}-\d{4}\b'),
     _fmt_dash_mdy),
    # Day Month Year: "15 March 2024"
    (re.compile(r'\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
                r'September|October|November|December)\s+\d{4}\b', re.I),
     _fmt_d_month_yyyy),
    # Month Year only: "March 2024"
    (re.compile(r'\b(?:January|February|March|April|May|June|July|August|'
                r'September|October|November|December)\s+\d{4}\b', re.I),
     _fmt_month_yyyy),
    # Short month + year: "Mar 2024"
    (re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{4}\b', re.I),
     _fmt_mon_yyyy),
    # Bare year on its own: "2019". Checked last so a year inside a fuller date
    # is handled by the pattern above it.
    (re.compile(r'^\s*\d{4}\s*$'),
     _fmt_year_only),
]


def _shift_date(date_str: str, offset_days: int) -> str:
    """
    Parse date_str, shift by offset_days, reformat in the original style.
    Returns the original string unchanged if parsing fails.
    """
    if not DATEUTIL_AVAILABLE:
        return date_str
    try:
        parsed = dateutil_parser.parse(date_str, fuzzy=True)
    except Exception:
        return date_str

    shifted = parsed + timedelta(days=offset_days)

    # Find the matching format pattern and reformat
    for pattern, formatter in _DATE_FORMAT_PATTERNS:
        if pattern.search(date_str):
            try:
                return formatter(shifted, date_str)
            except Exception:
                pass

    # Fallback: return ISO format
    return f"{shifted.year}-{shifted.month:02d}-{shifted.day:02d}"


def _doc_date_offset(text: str) -> int:
    """
    Generate a stable, per-document date shift offset (in days).
    Seeded by document content hash so re-running the same doc yields the same offset.
    Range: +180 to +730 days (shifts into the future, preserves all intervals).
    """
    h = int(hashlib.sha256(text[:4096].encode('utf-8', errors='replace')).hexdigest(), 16)
    return 180 + (h % 551)  # 180–730 days


def _get_engines():
    global _analyzer, _anonymizer
    if _analyzer is None:
        _analyzer = AnalyzerEngine()
        for recognizer in build_recognizers():
            _analyzer.registry.add_recognizer(recognizer)
        _anonymizer = AnonymizerEngine()
    return _analyzer, _anonymizer


def strip_pii(text: str, source_filename: str, output_mode: str = 'finetune') -> PIIResult:
    """
    output_mode='finetune': replace with Faker synthetic values (consistent within doc).
                            DATE_TIME entities are shifted by a fixed per-doc offset
                            so temporal relationships are preserved.
    output_mode='rag': replace with [ENTITY_TYPE] tokens.
    """
    analyzer, anonymizer = _get_engines()
    results = _clamp_over_long_spans(text, _analyze_chunked(analyzer, text))

    high = [r for r in results if r.score >= HIGH_CONFIDENCE]
    medium = [r for r in results if LOW_CONFIDENCE <= r.score < HIGH_CONFIDENCE]
    to_redact = high + medium

    review_flags = []
    for r in medium:
        ctx_start = max(0, r.start - 60)
        ctx_end = min(len(text), r.end + 60)
        review_flags.append({
            'file': source_filename,
            'entity_type': r.entity_type,
            'original_text': text[r.start:r.end],
            'confidence': round(r.score, 3),
            'context': text[ctx_start:ctx_end],
            'action': 'redacted_pending_review',
        })

    if not to_redact:
        return PIIResult(text=text, substitutions=0, review_flags=review_flags)

    # KTD1/KTD2: the library resolves overlapping spans by trimming at the
    # boundary and clamps its splice buffer against re-consuming text. The
    # hand-rolled replacement this replaces dropped the loser of an overlap in
    # finetune mode (leaving digits of an SSN behind) and spliced over it in
    # placeholder mode (producing '[PERSON]N]'). REMOVE_INTERSECTIONS is given
    # explicitly so the guarantee is structural rather than incidental.
    anonymized = anonymizer.anonymize(
        text=text,
        analyzer_results=to_redact,
        operators=_build_operators(text, output_mode),
        conflict_resolution=ConflictResolutionStrategy.REMOVE_INTERSECTIONS,
    )

    return PIIResult(
        text=anonymized.text,
        substitutions=len(anonymized.items),
        review_flags=review_flags,
    )


def _doc_faker(text: str) -> Faker:
    """A Faker seeded from the document's own content (KTD4).

    Faker.seed() sets a shared class-level seed, so a fixed module-level
    Faker.seed(0) made every document's first person 'Norma Fisher' -- measured
    across three unrelated documents. A fixed substitution scheme is inferable
    from a handful of outputs, which the de-identification literature treats as
    an assisted re-identification path. Seeding per document from the content
    hash keeps a re-run reproducible while varying across documents.
    """
    digest = hashlib.sha256(text[:4096].encode('utf-8', errors='replace')).hexdigest()
    fake = Faker()
    fake.seed_instance(int(digest[:16], 16))
    return fake


def _build_operators(text: str, output_mode: str) -> dict:
    """One operator per entity type.

    'rag' mode uses the library's own replace operator, so tokens are always
    well formed. 'finetune' mode uses a custom callable per type, memoised on
    its input string -- which is mandatory, not an optimisation. The library
    calls a custom operator twice per entity: first with the literal string
    'PII' to validate the operator, then with the real value (KTD3). A
    counter-based replacer therefore produced 'NAME4 met NAME2'. Keying the
    cache on the value makes the validation call a harmless throwaway entry and
    delivers within-document consistency at the same time.
    """
    if output_mode != 'finetune':
        return {
            entity: OperatorConfig("replace", {"new_value": f"[{entity}]"})
            for entity in ENTITY_TYPES
        }

    fake = _doc_faker(text)
    date_offset = _doc_date_offset(text)
    cache: dict = {}

    def _replacer(entity_type: str):
        def replace(value: str) -> str:
            if value not in cache:
                if entity_type == 'DATE_TIME':
                    cache[value] = _shift_date(value, date_offset)
                else:
                    cache[value] = _fake_value(entity_type, fake)
            return cache[value]
        return replace

    return {
        entity: OperatorConfig("custom", {"lambda": _replacer(entity)})
        for entity in ENTITY_TYPES
    }


def _analyze_chunked(analyzer, text: str) -> list:
    """Run the analyzer, chunking long documents to stay under spaCy's limit.

    The previous version cut at a fixed character count with no overlap and
    re-mapped offsets by hand, so an identifier straddling the cut was split in
    two and missed silently -- measured: an SSN across the boundary produced
    zero detections. presidio_analyzer.chunkers is public in the pinned 2.2.362
    and already does boundary-aware chunking with overlap, offset re-mapping
    and cross-chunk deduplication.
    """
    def predict(chunk_text: str) -> list:
        return analyzer.analyze(text=chunk_text, entities=ENTITY_TYPES, language='en')

    chunker = CharacterBasedTextChunker(
        chunk_size=MAX_PRESIDIO_CHARS,
        chunk_overlap=CHUNK_OVERLAP_CHARS,
    )
    return chunker.predict_with_chunking(text, predict)


# A trailing / leading plain-alphabetic word, which is what each half of a name
# wrapped by PDF extraction looks like.
_NAME_TOKEN_END = re.compile(r"[A-Za-z][A-Za-z'\-]*\Z")
_NAME_TOKEN_START = re.compile(r"\A[A-Za-z][A-Za-z'\-]*")
# A line that opens a new labelled field: "Medical Record Number: 4417392".
_LABELLED_FIELD = re.compile(r"\A[A-Za-z][A-Za-z0-9 .\-/']{0,40}:")


def _clamp_span(text: str, start: int, end: int) -> int:
    """Return an end offset that does not run past a line break into new text.

    The rule, in one sentence: a detection may cross at most one line break,
    and only when the words on both sides of it are plain alphabetic and the
    new line does not open a labelled field -- which is what a person's name
    wrapped across a soft break looks like, and nothing else is.

    Clamping at any newline would be too blunt, because a genuinely wrapped
    name is a legitimate multi-line span and truncating it would leak the
    second line.
    """
    breaks = [start + m.start() for m in re.finditer(r'\n', text[start:end])]
    for i, position in enumerate(breaks):
        before = text[start:position].rstrip(' \t')
        after = text[position + 1:end].lstrip(' \t')
        # The whole following line, not just the part inside the span: a span
        # ending mid-label would otherwise hide the colon that identifies it.
        line_after = text[position + 1:].split('\n', 1)[0].lstrip(' \t')
        wrapped_name = (
            i == 0
            and _NAME_TOKEN_END.search(before) is not None
            and _NAME_TOKEN_START.match(after) is not None
            and _LABELLED_FIELD.match(line_after) is None
        )
        if not wrapped_name:
            return position
    return end


def _clamp_over_long_spans(text: str, results: list) -> list:
    """Correct detections before they reach the anonymizer (R2).

    The library faithfully replaces whatever span it is given, so an over-long
    span has to be shortened here. spaCy labelled a URL, a line break and the
    following line's first word as one PERSON at 0.85, and replacement deleted
    all of it.
    """
    kept = []
    for r in results:
        clamped = _clamp_span(text, r.start, r.end)
        if clamped <= r.start:
            continue
        r.end = clamped
        kept.append(r)
    return kept


def _fake_value(entity_type: str, fake: Faker) -> str:
    """A type-correct synthetic stand-in (R4).

    Built lazily: the generators are only called for the type being replaced,
    so producing one fake name does not also consume a credit card number and
    an IP address from the document's seeded stream.
    """
    generators = {
        'PERSON':            fake.name,
        'PHONE_NUMBER':      fake.phone_number,
        'EMAIL_ADDRESS':     fake.email,
        'LOCATION':          lambda: fake.address().replace('\n', ', '),
        'US_SSN':            fake.ssn,
        'DATE_TIME':         fake.date,  # fallback only — normally _shift_date
        'US_BANK_NUMBER':    fake.bban,
        'CREDIT_CARD':       fake.credit_card_number,
        'US_PASSPORT':       lambda: (f"{''.join(fake.random_letters(2)).upper()}"
                                      f"{fake.numerify('#######')}"),
        'US_DRIVER_LICENSE': lambda: fake.numerify('D########'),
        'IP_ADDRESS':        fake.ipv4,
        'MEDICAL_LICENSE':   lambda: fake.numerify('ML#######'),
        'URL':               fake.url,
        'STREET_ADDRESS':    fake.street_address,
        'US_ZIP':            fake.postcode,
        # Safe Harbor (C) aggregates rather than replaces: a specific fake age
        # over 89 would be just as identifying as the real one.
        'AGE_OVER_89':       lambda: '90 or older',
        'MEDICAL_RECORD_NUMBER': lambda: fake.numerify('MRN-#######'),
        'HEALTH_PLAN_ID':    lambda: fake.numerify('MBR-########'),
        'ACCOUNT_NUMBER':    lambda: fake.numerify('ACCT-########'),
        'DEVICE_SERIAL':     lambda: fake.numerify('SN-####-??####').upper(),
        'VEHICLE_ID':        lambda: fake.numerify('???-####').upper(),
        'OTHER_IDENTIFIER':  lambda: fake.numerify('ID-########'),
        'UNRESOLVED_IDENTIFIER': lambda: fake.numerify('ID-########'),
    }
    generator = generators.get(entity_type)
    return generator() if generator else f"[{entity_type}]"
