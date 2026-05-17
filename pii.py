from dataclasses import dataclass, field
from typing import List, Optional

from faker import Faker
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_anonymizer import AnonymizerEngine


@dataclass
class PIIResult:
    text: str
    substitutions: int
    review_flags: List[dict] = field(default_factory=list)


HIGH_CONFIDENCE = 0.85
LOW_CONFIDENCE = 0.50

ENTITY_TYPES = [
    "PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION",
    "US_SSN", "DATE_TIME", "US_BANK_NUMBER", "CREDIT_CARD",
    "US_PASSPORT", "US_DRIVER_LICENSE", "IP_ADDRESS", "MEDICAL_LICENSE",
]

_analyzer: Optional[AnalyzerEngine] = None
_anonymizer: Optional[AnonymizerEngine] = None


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
    output_mode='rag': replace with [ENTITY_TYPE] tokens.
    """
    analyzer, _ = _get_engines()
    results = analyzer.analyze(text=text, entities=ENTITY_TYPES, language='en')

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


def _faker_replace(text: str, entities: list) -> str:
    fake = Faker()
    Faker.seed(0)
    substitution_table: dict = {}

    for r in sorted(entities, key=lambda x: x.start, reverse=True):
        original = text[r.start:r.end]
        if original not in substitution_table:
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
        'DATE_TIME':         fake.date(),
        'US_BANK_NUMBER':    fake.bban(),
        'CREDIT_CARD':       fake.credit_card_number(),
        'US_PASSPORT':       f"{''.join(fake.random_letters(2)).upper()}{fake.numerify('#######')}",
        'US_DRIVER_LICENSE': fake.numerify('D########'),
        'IP_ADDRESS':        fake.ipv4(),
        'MEDICAL_LICENSE':   fake.numerify('ML#######'),
    }
    return mapping.get(entity_type, f"[{entity_type}]")
