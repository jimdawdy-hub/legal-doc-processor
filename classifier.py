import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClassifyResult:
    doc_type: str  # 'caselaw' | 'published' | 'private' | 'uncertain'
    confidence: float
    signals: dict


CASELAW_SIGNALS = [
    (r'\d+\s+(?:F\.\d[dth]*|U\.S\.|S\.Ct\.|N\.E\.\d[dth]*|A\.\d[dth]*|'
     r'P\.\d[dth]*|Cal\.|N\.Y\.|Ill\.|Tex\.)\s+\d+', 0.40),
    (r'\b(?:OPINION|HELD|AFFIRMED|REVERSED|JUDGMENT|PER CURIAM|VACATED|REMANDED)\b', 0.30),
    (r'(?:WESTLAW|LEXISNEXIS|© \d{4} Thomson Reuters|WL \d+)', 0.20),
]

PUBLISHED_SIGNALS = [
    (r'\b(?:Vol\.|Volume)\s*\d+[,\s]+(?:No\.|Number|Issue)\s*\d+', 0.30),
    (r'(?:ISSN|ISBN)[:\s]+[\d\-X]+', 0.30),
    (r'\b(?:CLE|Continuing Legal Education|Law Review|Law Journal|Bar Journal|Bar Association)\b', 0.30),
    (r'©\s*\d{4}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}', 0.20),
]

PRIVATE_SIGNALS = [
    (r'(?:^|\n)(?:To|From|Subject|CC|BCC):\s+\S', 0.40),
    (r'\b(?:ATTORNEY FOR|ATTORNEYS FOR)\b', 0.35),
    (r'\b(?:PLAINTIFF|DEFENDANT|IN THE MATTER OF)\b', 0.35),
    (r'\bLAW DIVISION\b', 0.25),
    (r'(?:Dear\s+[A-Z][a-z]+|Sincerely,|Best regards,|Yours truly,|Kind regards,)', 0.30),
    (r'\b(?:CASE NOTE|CONFIDENTIAL|CLIENT FILE)\b', 0.25),
]


def classify(path: Path, text: str) -> ClassifyResult:
    """Classify a document by type from its extension and content.

    Note: .txt files have no extension shortcut and rely entirely on content
    signals. A .txt file containing email text won't get automatic 'private'
    classification the way .eml/.msg files do — it must match content patterns.
    """
    ext = path.suffix.lower()

    if ext in ('.eml', '.msg'):
        return ClassifyResult('private', 0.99, {'extension': ext})
    if ext == '.pptx':
        return ClassifyResult('published', 0.90, {'extension': ext})

    sample = text[:5000]
    scores = {'caselaw': 0.0, 'published': 0.0, 'private': 0.0}
    fired = {'caselaw': [], 'published': [], 'private': []}

    for pattern, weight in CASELAW_SIGNALS:
        if re.search(pattern, sample, re.IGNORECASE | re.MULTILINE):
            scores['caselaw'] += weight
            fired['caselaw'].append(pattern[:40])

    for pattern, weight in PUBLISHED_SIGNALS:
        if re.search(pattern, sample, re.IGNORECASE | re.MULTILINE):
            scores['published'] += weight
            fired['published'].append(pattern[:40])

    for pattern, weight in PRIVATE_SIGNALS:
        if re.search(pattern, sample, re.IGNORECASE | re.MULTILINE):
            scores['private'] += weight
            fired['private'].append(pattern[:40])

    total = sum(scores.values())
    if total == 0:
        return ClassifyResult('uncertain', 0.0, {'scores': scores, 'fired': fired})

    norm = {k: v / total for k, v in scores.items()}
    best_type = max(norm, key=norm.__getitem__)
    best_confidence = norm[best_type]

    if best_confidence < 0.60:
        return ClassifyResult('uncertain', best_confidence, {'scores': scores, 'fired': fired})

    return ClassifyResult(best_type, best_confidence, {'scores': scores, 'fired': fired})
