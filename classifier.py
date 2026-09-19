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


# The most any one category can score if every one of its signals fires.
# Confidence is reported as a share of this, so it stays comparable across
# categories and a lone weak signal can no longer normalise to a certainty.
ACHIEVABLE = {
    'caselaw': sum(w for _, w in CASELAW_SIGNALS),      # 0.90
    'published': sum(w for _, w in PUBLISHED_SIGNALS),  # 1.10
    'private': sum(w for _, w in PRIVATE_SIGNALS),      # 1.90
}

# Minimum raw evidence before a category is claimed at all. These are per
# category on purpose: the achievable totals above differ by more than 2x, so
# one shared cutoff would mean something different in each.
#
#   caselaw   0.30 — either signal is decisive alone. A reporter citation or
#                    the OPINION/AFFIRMED vocabulary means a court wrote it.
#   private   0.40 — an email header block is decisive; nothing weaker is.
#   published 0.50 — no single published signal is decisive. A copyright line,
#                    an ISSN and the words "Bar Association" are all ordinary
#                    furniture on medical paperwork, which is how a discharge
#                    summary came to read as 'published' at 100% confidence and
#                    skip the scrubber. Two signals required.
#
# Falling short lands in 'uncertain', which is scrubbed — so the failure
# direction of every threshold here is the safe one.
MIN_EVIDENCE = {'caselaw': 0.30, 'published': 0.50, 'private': 0.40}


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

    norm = {k: v / ACHIEVABLE[k] for k, v in scores.items()}
    qualified = [k for k, v in scores.items() if v >= MIN_EVIDENCE[k]]

    if not qualified:
        weak = max(norm, key=norm.__getitem__)
        return ClassifyResult('uncertain', norm[weak], {'scores': scores, 'fired': fired})

    best_type = max(qualified, key=lambda k: norm[k])
    return ClassifyResult(best_type, norm[best_type], {'scores': scores, 'fired': fired})
