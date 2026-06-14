import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from utils import sha256_file

from faker import Faker
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_anonymizer import AnonymizerEngine

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
LOW_CONFIDENCE = 0.50

ENTITY_TYPES = [
    "PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION",
    "US_SSN", "DATE_TIME", "US_BANK_NUMBER", "CREDIT_CARD",
    "US_PASSPORT", "US_DRIVER_LICENSE", "IP_ADDRESS", "MEDICAL_LICENSE",
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
        # Supplement built-in US_SSN with an explicit high-confidence pattern recognizer
        # so NNN-NN-NNNN is always caught regardless of Presidio's validation scoring
        _analyzer.registry.add_recognizer(PatternRecognizer(
            supported_entity="US_SSN",
            patterns=[Pattern("SSN_PATTERN", r'\b\d{3}-\d{2}-\d{4}\b', 0.90)],
            context=["ssn", "social security", "social security number"],
        ))
        _anonymizer = AnonymizerEngine()
    return _analyzer, _anonymizer


def strip_pii(text: str, source_filename: str, output_mode: str = 'finetune') -> PIIResult:
    """
    output_mode='finetune': replace with Faker synthetic values (consistent within doc).
                            DATE_TIME entities are shifted by a fixed per-doc offset
                            so temporal relationships are preserved.
    output_mode='rag': replace with [ENTITY_TYPE] tokens.
    """
    analyzer, _ = _get_engines()
    results = _analyze_chunked(analyzer, text)

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

    if output_mode == 'finetune':
        redacted_text = _faker_replace(text, to_redact)
    else:
        redacted_text = _token_replace(text, to_redact)

    return PIIResult(
        text=redacted_text,
        substitutions=len(to_redact),
        review_flags=review_flags,
    )


def _analyze_chunked(analyzer, text: str) -> list:
    """
    Run Presidio in chunks to avoid spaCy's 1M character limit.
    Adjusts entity start/end offsets by chunk position before returning.
    """
    if len(text) <= MAX_PRESIDIO_CHARS:
        return analyzer.analyze(text=text, entities=ENTITY_TYPES, language='en')

    all_results = []
    offset = 0
    while offset < len(text):
        chunk = text[offset:offset + MAX_PRESIDIO_CHARS]
        chunk_results = analyzer.analyze(text=chunk, entities=ENTITY_TYPES, language='en')
        for r in chunk_results:
            r.start += offset
            r.end += offset
        all_results.extend(chunk_results)
        offset += MAX_PRESIDIO_CHARS
    return all_results


def _deduplicate_overlaps(entities: list) -> list:
    """Remove overlapping entities, keeping the one that starts earliest."""
    if not entities:
        return entities
    sorted_ents = sorted(entities, key=lambda x: (x.start, -(x.end - x.start)))
    deduped = [sorted_ents[0]]
    for ent in sorted_ents[1:]:
        prev = deduped[-1]
        if ent.start < prev.end:
            continue
        deduped.append(ent)
    return deduped


def _faker_replace(text: str, entities: list) -> str:
    fake = Faker()
    Faker.seed(0)
    substitution_table: dict = {}
    date_offset = _doc_date_offset(text)
    entities = _deduplicate_overlaps(entities)

    for r in sorted(entities, key=lambda x: x.start, reverse=True):
        original = text[r.start:r.end]
        if original not in substitution_table:
            if r.entity_type == 'DATE_TIME':
                substitution_table[original] = _shift_date(original, date_offset)
            else:
                substitution_table[original] = _fake_value(r.entity_type, fake)
        text = text[:r.start] + substitution_table[original] + text[r.end:]

    return text


def _token_replace(text: str, entities: list) -> str:
    for r in sorted(entities, key=lambda x: x.start, reverse=True):
        token = f"[{r.entity_type}]"
        text = text[:r.start] + token + text[r.end:]
    return text


def _fake_value(entity_type: str, fake: Faker) -> str:
    mapping = {
        'PERSON':            fake.name(),
        'PHONE_NUMBER':      fake.phone_number(),
        'EMAIL_ADDRESS':     fake.email(),
        'LOCATION':          fake.address().replace('\n', ', '),
        'US_SSN':            fake.ssn(),
        'DATE_TIME':         fake.date(),  # fallback only — normally handled by _shift_date
        'US_BANK_NUMBER':    fake.bban(),
        'CREDIT_CARD':       fake.credit_card_number(),
        'US_PASSPORT':       f"{''.join(fake.random_letters(2)).upper()}{fake.numerify('#######')}",
        'US_DRIVER_LICENSE': fake.numerify('D########'),
        'IP_ADDRESS':        fake.ipv4(),
        'MEDICAL_LICENSE':   fake.numerify('ML#######'),
    }
    return mapping.get(entity_type, f"[{entity_type}]")
