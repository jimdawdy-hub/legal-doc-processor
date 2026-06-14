import re
from collections import Counter


def clean(text: str) -> str:
    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Strip Westlaw/Lexis annotation lines
    text = re.sub(
        r'(?m)^[^\n]*(?:Thomson Reuters|LexisNexis|Westlaw|© \d{4} Thomson Reuters)[^\n]*$',
        '',
        text
    )

    # Remove leading line numbers (pleadings/transcripts: "1  text", "10 text")
    # Tradeoff: this also strips legitimate numbered-list items in legal briefs
    # (e.g. "1. The plaintiff alleges..."). Acceptable for training data since
    # numbered-list formatting isn't meaningful to the model.
    text = re.sub(r'(?m)^\s*\d{1,3}\s{1,3}(?=\S)', '', text)

    # Normalize smart quotes and dashes
    text = text.replace('‘', "'").replace('’', "'")
    text = text.replace('“', '"').replace('”', '"')
    text = text.replace('–', '-').replace('—', '--')

    # Normalize ellipses
    text = text.replace('…', '...')

    # Strip trailing whitespace per line
    text = '\n'.join(line.rstrip() for line in text.split('\n'))

    # Strip repeated short lines appearing 3+ times (headers/footers)
    lines = text.split('\n')
    short_lines = [l.strip() for l in lines if l.strip() and len(l.strip()) < 80]
    repeated = {line for line, count in Counter(short_lines).items() if count >= 3}
    if repeated:
        lines = [l for l in lines if l.strip() not in repeated]
        text = '\n'.join(lines)

    # Collapse 3+ blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Collapse multiple spaces within lines
    text = re.sub(r'[ \t]{2,}', ' ', text)

    # Final blank line collapse
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()
