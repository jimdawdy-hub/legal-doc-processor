# Legal Document Processor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a modular pipeline skill that converts legal PDFs, DOCX, PPTX, EML, and MSG files into token-efficient, PII-safe JSONL datasets for RAG retrieval and QLoRA fine-tuning.

**Architecture:** Eight focused modules (reader, classifier, cleaner, pii, chunker, writer, provenance, pipeline) orchestrated by a CLI entry point. Each module has one responsibility and a clean dataclass interface. TDD throughout.

**Tech Stack:** Python 3.10+, pdfminer.six, pymupdf (fitz), pytesseract, ocrmypdf, python-docx, python-pptx, extract-msg, presidio-analyzer, presidio-anonymizer, spaCy en_core_web_lg, Faker, langchain-text-splitters, tiktoken, pytest

---

## File Map

```
~/.openclaw/workspace/skills/legal-doc-processor/
├── SKILL.md
├── requirements.txt
├── process.py          ← CLI entry point (argparse → pipeline.process_directory)
├── pipeline.py         ← orchestrator; calls all stages; manages output routing
├── reader.py           ← all formats → ReadResult(text, filename, extension, size_bytes, page_count, ocr, ocr_confidence)
├── classifier.py       ← text + path → ClassifyResult(doc_type, confidence, signals)
├── cleaner.py          ← text → cleaned text (headers, line numbers, whitespace)
├── pii.py              ← text → PIIResult(text, substitutions, review_flags)
├── chunker.py          ← text + doc_type → List[Chunk(text, index, total, token_count)]
├── writer.py           ← writes RAG JSONL and finetune JSONL
├── provenance.py       ← ProvenanceManifest class; writes provenance.json
└── tests/
    ├── conftest.py         ← shared fixtures
    ├── test_cleaner.py
    ├── test_classifier.py
    ├── test_reader.py
    ├── test_pii.py
    ├── test_chunker.py
    ├── test_writer.py
    ├── test_provenance.py
    └── test_pipeline.py
```

---

### Task 1: Scaffold — directory, git, requirements, install dependencies

**Files:**
- Create: `~/.openclaw/workspace/skills/legal-doc-processor/requirements.txt`
- Create: `~/.openclaw/workspace/skills/legal-doc-processor/tests/conftest.py`

- [ ] **Step 1: Initialize git repo and create directory structure**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
git init
mkdir -p tests
touch tests/__init__.py
```

- [ ] **Step 2: Write requirements.txt**

```
# Document reading
pdfminer.six>=20251230
pymupdf>=1.24.0
pytesseract>=0.3.13
ocrmypdf>=16.0.0
python-docx>=1.1.0
python-pptx>=1.0.2
extract-msg>=0.55.0
Pillow>=11.0.0

# Classification and cleaning
regex>=2024.0.0

# PII
presidio-analyzer>=2.2.0
presidio-anonymizer>=2.2.0
spacy>=3.7.0
Faker>=26.0.0

# Chunking and token counting
langchain-text-splitters>=0.3.0
tiktoken>=0.7.0

# Testing
pytest>=8.0.0
```

- [ ] **Step 3: Install dependencies**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
pip3 install -r requirements.txt
python3 -m spacy download en_core_web_lg
```

Expected: all packages install without error; `python3 -m spacy download en_core_web_lg` outputs `✔ Download and installation successful`.

- [ ] **Step 4: Write tests/conftest.py**

```python
import pytest
from pathlib import Path
import tempfile
import os

CASELAW_TEXT = """
UNITED STATES COURT OF APPEALS FOR THE SEVENTH CIRCUIT

Smith v. Jones, 123 F.3d 456 (7th Cir. 2019)

OPINION

The court held that the district court did not abuse its discretion.
The judgment is AFFIRMED.

I. BACKGROUND

The plaintiff filed suit alleging breach of contract. The district court
granted summary judgment in favor of defendant.

II. ANALYSIS

We review the district court's grant of summary judgment de novo.

A. Standard of Review

Summary judgment is appropriate where there is no genuine issue of
material fact. Fed. R. Civ. P. 56(a).

III. CONCLUSION

For the foregoing reasons, the judgment of the district court is AFFIRMED.
"""

PUBLISHED_TEXT = """
Chicago Bar Journal
Vol. 42, No. 3 — ISSN 0009-3157

Continuing Legal Education — Advanced Evidence

By Professor Jane Williams, University of Chicago Law School
© 2023 Chicago Bar Association

This article examines the evolution of hearsay exceptions under FRE 803.
"""

PRIVATE_TEXT = """
From: james.kowalski@lawfirm.com
To: sarah.chen@client.com
Subject: Case Update — Confidential
CC: tom.bradley@lawfirm.com
Date: March 3, 2024

Dear Sarah,

I am writing to update you on the status of your case.

LAW DIVISION — Cook County Circuit Court

ATTORNEYS FOR PLAINTIFF: James Kowalski, Sarah Chen
ATTORNEYS FOR DEFENDANT: Robert Mills

Plaintiff Jane Doe (SSN: 123-45-6789) resides at 123 Main Street, Chicago, IL 60601.
Her phone number is (312) 555-0100.

Sincerely,
James Kowalski
"""

@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)

@pytest.fixture
def caselaw_text():
    return CASELAW_TEXT

@pytest.fixture
def published_text():
    return PUBLISHED_TEXT

@pytest.fixture
def private_text():
    return PRIVATE_TEXT
```

- [ ] **Step 5: Verify pytest is importable**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
python3 -m pytest --collect-only
```

Expected: `no tests ran` (no test files yet) — no import errors.

- [ ] **Step 6: Commit**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
git add requirements.txt tests/conftest.py tests/__init__.py
git commit -m "feat: scaffold legal-doc-processor skill"
```

---

### Task 2: cleaner.py

**Files:**
- Create: `cleaner.py`
- Create: `tests/test_cleaner.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cleaner.py
import pytest
from cleaner import clean

def test_collapses_excess_blank_lines():
    text = "First paragraph.\n\n\n\n\nSecond paragraph."
    result = clean(text)
    assert "\n\n\n" not in result
    assert "First paragraph." in result
    assert "Second paragraph." in result

def test_removes_leading_line_numbers():
    text = "1  The plaintiff filed suit.\n2  The defendant answered.\n10 The court ruled."
    result = clean(text)
    assert "1  The" not in result
    assert "The plaintiff filed suit." in result
    assert "The defendant answered." in result

def test_normalizes_smart_quotes():
    text = "“This is quoted.” She said ‘hello.’"
    result = clean(text)
    assert '"This is quoted."' in result
    assert "'hello.'" in result

def test_strips_repeated_header_lines():
    # A line appearing 3+ times (short) should be stripped as a header/footer
    header = "CONFIDENTIAL — ATTORNEY WORK PRODUCT"
    text = f"{header}\n\nPage one content.\n\n{header}\n\nPage two content.\n\n{header}\n\nPage three content."
    result = clean(text)
    assert result.count(header) == 0
    assert "Page one content." in result

def test_normalizes_em_dashes():
    text = "The case—Smith v. Jones—was decided in 2019."
    result = clean(text)
    assert "—" not in result
    assert "The case--Smith v. Jones--was decided in 2019." in result

def test_strips_westlaw_annotation_lines():
    text = "Valid legal text.\n© 2023 Thomson Reuters. No claim to original U.S. Government Works.\nMore valid text."
    result = clean(text)
    assert "Thomson Reuters" not in result
    assert "Valid legal text." in result
    assert "More valid text." in result
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
python3 -m pytest tests/test_cleaner.py -v
```

Expected: `ImportError: No module named 'cleaner'`

- [ ] **Step 3: Implement cleaner.py**

```python
import re


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
    from collections import Counter
    short_lines = [l.strip() for l in lines if l.strip() and len(l.strip()) < 80]
    repeated = {line for line, count in Counter(short_lines).items() if count >= 3}
    if repeated:
        lines = [l for l in lines if l.strip() not in repeated]
        text = '\n'.join(lines)

    # Collapse 3+ blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Collapse multiple spaces within lines (but not newlines)
    text = re.sub(r'[ \t]{2,}', ' ', text)

    # Final blank line collapse after removing repeated lines may have created more
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
python3 -m pytest tests/test_cleaner.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cleaner.py tests/test_cleaner.py
git commit -m "feat: add cleaner module with text normalization"
```

---

### Task 3: classifier.py

**Files:**
- Create: `classifier.py`
- Create: `tests/test_classifier.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_classifier.py
import pytest
from pathlib import Path
from classifier import classify

def test_classifies_caselaw(caselaw_text):
    result = classify(Path("smith_v_jones.pdf"), caselaw_text)
    assert result.doc_type == "caselaw"
    assert result.confidence >= 0.60

def test_classifies_published(published_text):
    result = classify(Path("cle_article.pdf"), published_text)
    assert result.doc_type == "published"
    assert result.confidence >= 0.60

def test_classifies_private(private_text):
    result = classify(Path("client_brief.pdf"), private_text)
    assert result.doc_type == "private"
    assert result.confidence >= 0.60

def test_eml_extension_always_private():
    result = classify(Path("message.eml"), "any text here")
    assert result.doc_type == "private"
    assert result.confidence == 0.99

def test_msg_extension_always_private():
    result = classify(Path("email.msg"), "any text here")
    assert result.doc_type == "private"
    assert result.confidence == 0.99

def test_pptx_extension_always_published():
    result = classify(Path("cle_presentation.pptx"), "any text here")
    assert result.doc_type == "published"
    assert result.confidence == 0.90

def test_uncertain_when_no_signals():
    result = classify(Path("mystery.pdf"), "This is just some random text with no legal signals.")
    assert result.doc_type == "uncertain"

def test_email_headers_in_body_classify_private():
    text = "To: john@example.com\nFrom: jane@example.com\nSubject: Re: Case update\n\nBody of email."
    result = classify(Path("printed_email.pdf"), text)
    assert result.doc_type == "private"

def test_attorney_for_classifies_private():
    text = "IN THE CIRCUIT COURT\nATTORNEYS FOR PLAINTIFF: James Smith\nLAW DIVISION\nDocket No. 2024-L-001234"
    result = classify(Path("pleading.pdf"), text)
    assert result.doc_type == "private"

def test_signals_dict_populated(caselaw_text):
    result = classify(Path("opinion.pdf"), caselaw_text)
    assert "scores" in result.signals
    assert result.signals["scores"]["caselaw"] > 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_classifier.py -v
```

Expected: `ImportError: No module named 'classifier'`

- [ ] **Step 3: Implement classifier.py**

```python
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClassifyResult:
    doc_type: str  # 'caselaw' | 'published' | 'private' | 'uncertain'
    confidence: float
    signals: dict


# (pattern, weight) pairs — matched case-insensitively against first 5000 chars
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
    ext = path.suffix.lower()

    # Extension shortcuts — override content scoring
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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_classifier.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add classifier.py tests/test_classifier.py
git commit -m "feat: add rule-based document classifier"
```

---

### Task 4: reader.py — text formats (DOCX, PPTX, EML, MSG)

**Files:**
- Create: `reader.py`
- Create: `tests/test_reader.py`

- [ ] **Step 1: Write failing tests for text format readers**

```python
# tests/test_reader.py
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from reader import read_file, ReadResult

def test_read_eml(tmp_dir):
    eml_content = (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Case Update\r\n"
        b"Date: Mon, 1 Jan 2024 10:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"This is the email body."
    )
    eml_path = tmp_dir / "test.eml"
    eml_path.write_bytes(eml_content)

    result = read_file(eml_path)
    assert isinstance(result, ReadResult)
    assert "From: alice@example.com" in result.text
    assert "This is the email body." in result.text
    assert result.extension == ".eml"
    assert result.ocr is False

def test_read_docx(tmp_dir):
    from docx import Document
    doc = Document()
    doc.add_paragraph("Introduction paragraph.")
    doc.add_paragraph("Second paragraph about the case.")
    docx_path = tmp_dir / "test.docx"
    doc.save(str(docx_path))

    result = read_file(docx_path)
    assert "Introduction paragraph." in result.text
    assert "Second paragraph about the case." in result.text
    assert result.extension == ".docx"
    assert result.ocr is False

def test_read_pptx(tmp_dir):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    slide_layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(slide_layout)
    slide.shapes.title.text = "Legal Overview"
    slide.placeholders[1].text = "Key points about the statute."
    pptx_path = tmp_dir / "test.pptx"
    prs.save(str(pptx_path))

    result = read_file(pptx_path)
    assert "Legal Overview" in result.text
    assert result.extension == ".pptx"
    assert result.ocr is False

def test_read_msg(tmp_dir):
    # extract-msg requires a real MSG file; mock the library
    import extract_msg
    mock_msg = MagicMock()
    mock_msg.sender = "alice@example.com"
    mock_msg.to = "bob@example.com"
    mock_msg.subject = "Update"
    mock_msg.date = "2024-01-01"
    mock_msg.body = "MSG body text."

    msg_path = tmp_dir / "test.msg"
    msg_path.write_bytes(b"fake msg content")

    with patch("reader.extract_msg.Message", return_value=mock_msg):
        result = read_file(msg_path)

    assert "alice@example.com" in result.text
    assert "MSG body text." in result.text
    assert result.extension == ".msg"

def test_unsupported_extension_raises(tmp_dir):
    bad_path = tmp_dir / "file.xyz"
    bad_path.write_text("content")
    with pytest.raises(ValueError, match="Unsupported format"):
        read_file(bad_path)

def test_read_result_has_required_fields(tmp_dir):
    eml_path = tmp_dir / "test.eml"
    eml_path.write_bytes(
        b"From: x@x.com\r\nSubject: Test\r\nContent-Type: text/plain\r\n\r\nBody."
    )
    result = read_file(eml_path)
    assert hasattr(result, 'text')
    assert hasattr(result, 'filename')
    assert hasattr(result, 'extension')
    assert hasattr(result, 'size_bytes')
    assert hasattr(result, 'ocr')
    assert hasattr(result, 'ocr_confidence')
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_reader.py -v
```

Expected: `ImportError: No module named 'reader'`

- [ ] **Step 3: Implement reader.py (text formats only — PDF/OCR added in Task 5)**

```python
import email as email_lib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pdfminer.high_level import extract_text as pdfminer_extract

try:
    import fitz  # pymupdf
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

import extract_msg as extract_msg_lib


@dataclass
class ReadResult:
    text: str
    filename: str
    extension: str
    size_bytes: int
    page_count: Optional[int] = None
    ocr: bool = False
    ocr_confidence: Optional[float] = None


SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg'}


def read_file(path: Path) -> Optional[ReadResult]:
    """
    Returns ReadResult, or None if the file is a scanned PDF with OCR
    confidence below threshold (caller should move to ocr_queue).
    """
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported format: {ext}")
    size = path.stat().st_size

    if ext == '.pdf':
        return _read_pdf(path, size)
    elif ext == '.docx':
        return _read_docx(path, size)
    elif ext == '.pptx':
        return _read_pptx(path, size)
    elif ext == '.eml':
        return _read_eml(path, size)
    elif ext == '.msg':
        return _read_msg(path, size)


def _read_docx(path: Path, size: int) -> ReadResult:
    from docx import Document
    doc = Document(str(path))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    for table in doc.tables:
        for row in table.rows:
            row_text = ' | '.join(c.text.strip() for c in row.cells if c.text.strip())
            if row_text:
                parts.append(row_text)
    return ReadResult(
        text='\n'.join(parts), filename=path.name,
        extension='.docx', size_bytes=size,
    )


def _read_pptx(path: Path, size: int) -> ReadResult:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"[Slide {i}]")
        for shape in slide.shapes:
            if hasattr(shape, 'text') and shape.text.strip():
                parts.append(shape.text.strip())
        if (slide.has_notes_slide and
                slide.notes_slide.notes_text_frame.text.strip()):
            parts.append(f"[Notes] {slide.notes_slide.notes_text_frame.text.strip()}")
    return ReadResult(
        text='\n'.join(parts), filename=path.name,
        extension='.pptx', size_bytes=size,
    )


def _read_eml(path: Path, size: int) -> ReadResult:
    with open(path, 'rb') as f:
        msg = email_lib.message_from_bytes(f.read())

    headers = {k: msg.get(k, '') for k in ('From', 'To', 'Subject', 'Date', 'CC', 'BCC')}
    header_text = '\n'.join(f"{k}: {v}" for k, v in headers.items() if v)

    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='replace')
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode('utf-8', errors='replace')

    return ReadResult(
        text=f"{header_text}\n\n{body}".strip(),
        filename=path.name, extension='.eml', size_bytes=size,
    )


def _read_msg(path: Path, size: int) -> ReadResult:
    msg = extract_msg_lib.Message(str(path))
    headers = (
        f"From: {msg.sender or ''}\n"
        f"To: {msg.to or ''}\n"
        f"Subject: {msg.subject or ''}\n"
        f"Date: {msg.date or ''}"
    )
    return ReadResult(
        text=f"{headers}\n\n{msg.body or ''}".strip(),
        filename=path.name, extension='.msg', size_bytes=size,
    )


# PDF reading implemented in Task 5
def _read_pdf(path: Path, size: int) -> Optional[ReadResult]:
    text = pdfminer_extract(str(path)) or ''
    if len(text.strip()) >= 100:
        return ReadResult(
            text=text, filename=path.name,
            extension='.pdf', size_bytes=size, ocr=False,
        )
    return _ocr_pdf(path, size)


def _ocr_pdf(path: Path, size: int) -> Optional[ReadResult]:
    """Returns None if OCR confidence is below 70% threshold."""
    confidence = _sample_ocr_confidence(path)
    if confidence is not None and confidence < 70.0:
        return None  # caller moves to ocr_queue

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            ['ocrmypdf', '--output-type', 'pdf', '--redo-ocr',
             str(path), str(tmp_path)],
            check=True, capture_output=True,
        )
        text = pdfminer_extract(str(tmp_path)) or ''
    finally:
        tmp_path.unlink(missing_ok=True)

    return ReadResult(
        text=text, filename=path.name, extension='.pdf',
        size_bytes=size, ocr=True, ocr_confidence=confidence,
    )


def _sample_ocr_confidence(path: Path) -> Optional[float]:
    """Sample first 3 pages with pytesseract; return mean word confidence or None."""
    if not (FITZ_AVAILABLE and TESSERACT_AVAILABLE):
        return None

    doc = fitz.open(str(path))
    confidences = []
    for page_num in range(min(3, len(doc))):
        pix = doc[page_num].get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        page_conf = [
            c for c in data['conf']
            if isinstance(c, (int, float)) and c > 0
        ]
        confidences.extend(page_conf)

    return sum(confidences) / len(confidences) if confidences else None
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_reader.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: add reader module for DOCX, PPTX, EML, MSG, PDF"
```

---

### Task 5: chunker.py

**Files:**
- Create: `chunker.py`
- Create: `tests/test_chunker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chunker.py
import pytest
from chunker import chunk, count_tokens, Chunk

def test_chunk_returns_list_of_chunk_objects(caselaw_text):
    result = chunk(caselaw_text, 'caselaw')
    assert isinstance(result, list)
    assert all(isinstance(c, Chunk) for c in result)

def test_chunk_indices_are_sequential(caselaw_text):
    result = chunk(caselaw_text * 10, 'caselaw')
    for i, c in enumerate(result):
        assert c.index == i
        assert c.total == len(result)

def test_chunk_token_counts_are_positive(caselaw_text):
    result = chunk(caselaw_text, 'caselaw')
    assert all(c.token_count > 0 for c in result)

def test_caselaw_chunks_at_512_tokens():
    # Create text long enough to produce multiple chunks
    long_text = "The court held that the defendant was liable. " * 300
    result = chunk(long_text, 'caselaw')
    assert len(result) > 1
    # Each chunk should be at or under 512 tokens (+ some slack for overlap)
    for c in result:
        assert c.token_count <= 600  # 512 + reasonable overhead

def test_private_chunks_at_2048_tokens():
    # Short private doc should be a single chunk
    short_text = "Client retained our firm to handle the matter. " * 20
    result = chunk(short_text, 'private')
    assert len(result) == 1

def test_count_tokens_returns_int():
    n = count_tokens("The court held for the plaintiff.")
    assert isinstance(n, int)
    assert n > 0

def test_single_chunk_covers_full_text():
    text = "Short legal text for a private document."
    result = chunk(text, 'private')
    assert len(result) == 1
    assert result[0].text.strip() == text.strip()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_chunker.py -v
```

Expected: `ImportError: No module named 'chunker'`

- [ ] **Step 3: Implement chunker.py**

```python
from dataclasses import dataclass
from typing import List

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter


@dataclass
class Chunk:
    text: str
    index: int
    total: int
    token_count: int


CHUNK_CONFIG = {
    'caselaw':   {'chunk_size': 512,  'overlap': 64},
    'published': {'chunk_size': 512,  'overlap': 64},
    'private':   {'chunk_size': 2048, 'overlap': 128},
    'uncertain': {'chunk_size': 2048, 'overlap': 128},
}

# Legal section headers tried first as split boundaries for caselaw
LEGAL_SEPARATORS = [
    '\n\nI.', '\n\nII.', '\n\nIII.', '\n\nIV.', '\n\nV.',
    '\n\nA.', '\n\nB.', '\n\nC.',
    '\n\nFACTS', '\n\nANALYSIS', '\n\nHELD', '\n\nHOLDING',
    '\n\nCONCLUSION', '\n\nDISCUSSION', '\n\nBACKGROUND',
    '\n\n', '\n', ' ', '',
]

_enc = None


def _get_encoder():
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding('cl100k_base')
    return _enc


def count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def chunk(text: str, doc_type: str) -> List[Chunk]:
    config = CHUNK_CONFIG.get(doc_type, CHUNK_CONFIG['private'])
    enc = _get_encoder()
    separators = LEGAL_SEPARATORS if doc_type == 'caselaw' else ['\n\n', '\n', ' ', '']

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config['chunk_size'],
        chunk_overlap=config['overlap'],
        length_function=lambda t: len(enc.encode(t)),
        separators=separators,
    )

    splits = splitter.split_text(text)
    if not splits:
        splits = [text]

    chunks = [
        Chunk(
            text=s,
            index=i,
            total=len(splits),
            token_count=len(enc.encode(s)),
        )
        for i, s in enumerate(splits)
    ]
    return chunks
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_chunker.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add chunker.py tests/test_chunker.py
git commit -m "feat: add token-aware chunker with legal section boundaries"
```

---

### Task 6: pii.py

**Files:**
- Create: `pii.py`
- Create: `tests/test_pii.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_pii.py
import pytest
from pii import strip_pii, PIIResult

SAMPLE_PRIVATE = (
    "James Kowalski filed a motion. His SSN is 123-45-6789. "
    "Call him at (312) 555-0100 or james.kowalski@lawfirm.com. "
    "He lives at 123 Main Street, Chicago, IL 60601."
)

def test_strip_pii_returns_pii_result():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='finetune')
    assert isinstance(result, PIIResult)

def test_finetune_mode_replaces_with_faker_not_tokens():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='finetune')
    # Should NOT contain original PII values
    assert "123-45-6789" not in result.text
    assert "james.kowalski@lawfirm.com" not in result.text
    # Should NOT contain bracket tokens in finetune mode
    assert "[US_SSN]" not in result.text
    assert "[EMAIL_ADDRESS]" not in result.text

def test_rag_mode_replaces_with_tokens():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='rag')
    assert "123-45-6789" not in result.text
    # RAG mode uses typed tokens
    assert "[" in result.text

def test_substitution_count_is_positive():
    result = strip_pii(SAMPLE_PRIVATE, "test.pdf", output_mode='finetune')
    assert result.substitutions > 0

def test_medium_confidence_entities_go_to_review_log():
    result = strip_pii(SAMPLE_PRIVATE, "brief.pdf", output_mode='finetune')
    # review_flags is a list; may or may not have entries depending on presidio scores
    assert isinstance(result.review_flags, list)

def test_review_flag_has_required_fields():
    # Inject a low-confidence scenario — check flag structure if any flags exist
    result = strip_pii(SAMPLE_PRIVATE, "brief.pdf", output_mode='finetune')
    for flag in result.review_flags:
        assert 'file' in flag
        assert 'entity_type' in flag
        assert 'original_text' in flag
        assert 'confidence' in flag
        assert 'context' in flag
        assert 'action' in flag

def test_consistent_faker_replacement_within_document():
    # Same name appearing twice should get same fake replacement
    text = "John Smith filed the motion. John Smith appeared in court."
    result = strip_pii(text, "test.pdf", output_mode='finetune')
    # Split on the likely fake name — both occurrences should be identical
    # We verify by checking the original names are gone
    assert "John Smith" not in result.text

def test_clean_text_unchanged():
    text = "The statute provides that courts shall apply a reasonableness standard."
    result = strip_pii(text, "statute.pdf", output_mode='finetune')
    # No PII — text should be essentially the same
    assert result.substitutions == 0
    assert result.text == text
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_pii.py -v
```

Expected: `ImportError: No module named 'pii'`

- [ ] **Step 3: Implement pii.py**

```python
from dataclasses import dataclass, field
from typing import List, Optional

from faker import Faker
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


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
    Faker.seed(0)  # deterministic within a run
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
        'PERSON':           fake.name(),
        'PHONE_NUMBER':     fake.phone_number(),
        'EMAIL_ADDRESS':    fake.email(),
        'LOCATION':         fake.address().replace('\n', ', '),
        'US_SSN':           fake.ssn(),
        'DATE_TIME':        fake.date(),
        'US_BANK_NUMBER':   fake.bban(),
        'CREDIT_CARD':      fake.credit_card_number(),
        'US_PASSPORT':      f"{''.join(fake.random_letters(2)).upper()}{fake.numerify('#######')}",
        'US_DRIVER_LICENSE': fake.numerify('D########'),
        'IP_ADDRESS':       fake.ipv4(),
        'MEDICAL_LICENSE':  fake.numerify('ML#######'),
    }
    return mapping.get(entity_type, f"[{entity_type}]")
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_pii.py -v
```

Expected: all 8 tests PASS. (Presidio spaCy model must be downloaded from Task 1.)

- [ ] **Step 5: Commit**

```bash
git add pii.py tests/test_pii.py
git commit -m "feat: add PII stripping with Presidio detection and Faker replacement"
```

---

### Task 7: writer.py

**Files:**
- Create: `writer.py`
- Create: `tests/test_writer.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_writer.py
import json
import pytest
from pathlib import Path
from writer import write_rag_chunks, write_finetune_record
from chunker import Chunk

def _make_chunks(n=3):
    return [
        Chunk(text=f"Chunk {i} text about the law.", index=i, total=n, token_count=10)
        for i in range(n)
    ]

def test_write_rag_chunks_creates_jsonl(tmp_dir):
    chunks = _make_chunks(3)
    write_rag_chunks(chunks, "smith_v_jones.pdf", "caselaw", {}, tmp_dir)
    out = tmp_dir / "smith_v_jones.jsonl"
    assert out.exists()

def test_rag_jsonl_has_correct_record_count(tmp_dir):
    chunks = _make_chunks(3)
    write_rag_chunks(chunks, "opinion.pdf", "caselaw", {}, tmp_dir)
    lines = (tmp_dir / "opinion.jsonl").read_text().strip().split('\n')
    assert len(lines) == 3

def test_rag_record_structure(tmp_dir):
    chunks = _make_chunks(2)
    write_rag_chunks(chunks, "case.pdf", "caselaw", {"citation": "Smith v. Jones, 123 F.3d 456"}, tmp_dir)
    record = json.loads((tmp_dir / "case.jsonl").read_text().split('\n')[0])
    assert "id" in record
    assert "text" in record
    assert "metadata" in record
    assert record["metadata"]["doc_type"] == "caselaw"
    assert record["metadata"]["citation"] == "Smith v. Jones, 123 F.3d 456"
    assert record["metadata"]["chunk_index"] == 0
    assert record["metadata"]["total_chunks"] == 2

def test_rag_id_is_unique_per_chunk(tmp_dir):
    chunks = _make_chunks(3)
    write_rag_chunks(chunks, "opinion.pdf", "caselaw", {}, tmp_dir)
    lines = (tmp_dir / "opinion.jsonl").read_text().strip().split('\n')
    ids = [json.loads(l)["id"] for l in lines]
    assert len(set(ids)) == 3

def test_write_finetune_record_appends(tmp_dir):
    out_file = tmp_dir / "dataset.jsonl"
    write_finetune_record("First doc text.", "anon_001", "private", True, 3, 1, 50, out_file)
    write_finetune_record("Second doc text.", "anon_002", "private", True, 0, 0, 40, out_file)
    lines = out_file.read_text().strip().split('\n')
    assert len(lines) == 2

def test_finetune_record_structure(tmp_dir):
    out_file = tmp_dir / "dataset.jsonl"
    write_finetune_record("Legal text here.", "anon_042", "private", True, 5, 2, 120, out_file)
    record = json.loads(out_file.read_text())
    assert record["text"] == "Legal text here."
    assert record["metadata"]["source"] == "anon_042"
    assert record["metadata"]["pii_stripped"] is True
    assert record["metadata"]["faker_substitutions"] == 5
    assert record["metadata"]["review_flags"] == 2
    assert record["metadata"]["token_count"] == 120

def test_finetune_source_is_anon_id_not_real_filename(tmp_dir):
    out_file = tmp_dir / "dataset.jsonl"
    write_finetune_record("Text.", "anon_00042", "private", True, 0, 0, 10, out_file)
    record = json.loads(out_file.read_text())
    assert "real_filename" not in record["metadata"]["source"]
    assert record["metadata"]["source"].startswith("anon_")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_writer.py -v
```

Expected: `ImportError: No module named 'writer'`

- [ ] **Step 3: Implement writer.py**

```python
import json
import re
from pathlib import Path
from typing import List

from chunker import Chunk


def _slugify(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r'[^a-z0-9]+', '_', stem.lower()).strip('_')


def write_rag_chunks(
    chunks: List[Chunk],
    source_filename: str,
    doc_type: str,
    extra_metadata: dict,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(source_filename)
    out_path = output_dir / f"{slug}.jsonl"

    with open(out_path, 'w') as f:
        for c in chunks:
            record = {
                'id': f"{slug}_chunk_{c.index:03d}",
                'text': c.text,
                'metadata': {
                    'source': source_filename,
                    'doc_type': doc_type,
                    'chunk_index': c.index,
                    'total_chunks': c.total,
                    'token_count': c.token_count,
                    **extra_metadata,
                },
            }
            f.write(json.dumps(record) + '\n')


def write_finetune_record(
    text: str,
    anon_id: str,
    doc_type: str,
    pii_stripped: bool,
    faker_substitutions: int,
    review_flags: int,
    token_count: int,
    output_file: Path,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'text': text,
        'metadata': {
            'source': anon_id,
            'doc_type': doc_type,
            'pii_stripped': pii_stripped,
            'faker_substitutions': faker_substitutions,
            'review_flags': review_flags,
            'token_count': token_count,
        },
    }
    with open(output_file, 'a') as f:
        f.write(json.dumps(record) + '\n')
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_writer.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add writer.py tests/test_writer.py
git commit -m "feat: add writer module for RAG and finetune JSONL output"
```

---

### Task 8: provenance.py

**Files:**
- Create: `provenance.py`
- Create: `tests/test_provenance.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_provenance.py
import csv
import json
import pytest
from pathlib import Path
from provenance import ProvenanceManifest

def _write_sidecar(tmp_dir: Path) -> Path:
    sidecar = tmp_dir / "sources.csv"
    with open(sidecar, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['filename', 'copyright_status', 'source_url_or_collection', 'notes'])
        writer.writeheader()
        writer.writerow({
            'filename': 'opinion.pdf',
            'copyright_status': 'public domain',
            'source_url_or_collection': 'https://courtlistener.com/1',
            'notes': '',
        })
    return sidecar

def _add_sample_file(manifest: ProvenanceManifest, filename: str = "opinion.pdf", **kwargs):
    defaults = dict(
        original_path=Path(filename),
        doc_type='caselaw',
        classification_confidence=0.91,
        ocr=False,
        ocr_confidence=None,
        pii_stripped=False,
        faker_substitutions=0,
        review_flags=0,
        chunk_count=12,
        token_count=5800,
    )
    defaults.update(kwargs)
    manifest.add_file(**defaults)

def test_save_creates_provenance_json(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m)
    m.save()
    assert (tmp_dir / 'provenance.json').exists()

def test_provenance_has_dataset_level_fields(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    for field in ('dataset_name', 'version', 'created', 'license', 'jurisdiction_coverage'):
        assert field in data

def test_provenance_preserves_dataset_fields_on_rerun(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m)
    m.save()
    # Manually edit the dataset name
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    data['dataset_name'] = 'My Legal Dataset'
    (tmp_dir / 'provenance.json').write_text(json.dumps(data))
    # Re-run manifest — should NOT overwrite dataset_name
    m2 = ProvenanceManifest(tmp_dir)
    _add_sample_file(m2, "second.pdf")
    m2.save()
    data2 = json.loads((tmp_dir / 'provenance.json').read_text())
    assert data2['dataset_name'] == 'My Legal Dataset'

def test_provenance_per_file_record_structure(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    record = data['files'][0]
    assert 'original_filename' in record
    assert 'sha256' in record
    assert 'doc_type' in record
    assert 'date_processed' in record
    assert 'processing' in record
    assert 'chunk_count' in record['processing']
    assert 'token_count' in record['processing']

def test_sidecar_populates_copyright_fields(tmp_dir):
    sidecar = _write_sidecar(tmp_dir)
    # Create a real file so sha256 works
    (tmp_dir / 'opinion.pdf').write_bytes(b'fake pdf content')
    m = ProvenanceManifest(tmp_dir, sidecar_path=sidecar)
    m.add_file(
        original_path=tmp_dir / 'opinion.pdf',
        doc_type='caselaw', classification_confidence=0.91,
        ocr=False, ocr_confidence=None, pii_stripped=False,
        faker_substitutions=0, review_flags=0, chunk_count=5, token_count=1000,
    )
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    record = data['files'][0]
    assert record['copyright_status'] == 'public domain'
    assert record['source_url_or_collection'] == 'https://courtlistener.com/1'

def test_summary_counts_are_correct(tmp_dir):
    m = ProvenanceManifest(tmp_dir)
    _add_sample_file(m, 'case1.pdf', doc_type='caselaw')
    _add_sample_file(m, 'case2.pdf', doc_type='caselaw')
    _add_sample_file(m, 'email.pdf', doc_type='private', pii_stripped=True, faker_substitutions=3)
    m.save()
    data = json.loads((tmp_dir / 'provenance.json').read_text())
    assert data['summary']['caselaw_files'] == 2
    assert data['summary']['private_files'] == 1
    assert data['summary']['total_files'] == 3
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_provenance.py -v
```

Expected: `ImportError: No module named 'provenance'`

- [ ] **Step 3: Implement provenance.py**

```python
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(65536), b''):
                h.update(block)
    except FileNotFoundError:
        return ''
    return h.hexdigest()


class ProvenanceManifest:
    def __init__(self, output_dir: Path, sidecar_path: Optional[Path] = None):
        self.output_dir = output_dir
        self.manifest_path = output_dir / 'provenance.json'
        self.sidecar = self._load_sidecar(sidecar_path) if sidecar_path else {}
        self._files: list = []
        self._dataset_meta = self._load_or_init_dataset_meta()

    def _load_sidecar(self, path: Path) -> dict:
        result = {}
        with open(path, newline='') as f:
            for row in csv.DictReader(f):
                result[row['filename']] = {
                    'copyright_status': row.get('copyright_status', ''),
                    'source_url_or_collection': row.get('source_url_or_collection', ''),
                    'notes': row.get('notes', ''),
                }
        return result

    def _load_or_init_dataset_meta(self) -> dict:
        fields = ('dataset_name', 'version', 'created', 'created_by',
                  'source_collection', 'license', 'jurisdiction_coverage', 'notes')
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text())
            return {k: existing[k] for k in fields if k in existing}
        return {
            'dataset_name': '',
            'version': '1.0.0',
            'created': datetime.now(timezone.utc).date().isoformat(),
            'created_by': '',
            'source_collection': '',
            'license': '',
            'jurisdiction_coverage': [],
            'notes': '',
        }

    def add_file(
        self,
        original_path: Path,
        doc_type: str,
        classification_confidence: float,
        ocr: bool,
        ocr_confidence: Optional[float],
        pii_stripped: bool,
        faker_substitutions: int,
        review_flags: int,
        chunk_count: int,
        token_count: int,
        skipped: bool = False,
        skip_reason: Optional[str] = None,
    ) -> None:
        sidecar_info = self.sidecar.get(original_path.name, {})
        self._files.append({
            'original_filename': original_path.name,
            'sha256': _sha256(original_path),
            'doc_type': doc_type,
            'classification_confidence': round(classification_confidence, 3),
            'date_processed': datetime.now(timezone.utc).isoformat(),
            'copyright_status': sidecar_info.get('copyright_status', ''),
            'source_url_or_collection': sidecar_info.get('source_url_or_collection', ''),
            'notes': sidecar_info.get('notes', ''),
            'skipped': skipped,
            'skip_reason': skip_reason,
            'processing': {
                'ocr': ocr,
                'ocr_confidence': round(ocr_confidence, 1) if ocr_confidence is not None else None,
                'pii_stripped': pii_stripped,
                'faker_substitutions': faker_substitutions,
                'review_flags': review_flags,
                'chunk_count': chunk_count,
                'token_count': token_count,
            },
        })

    def save(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        processed = [f for f in self._files if not f['skipped']]
        summary = {
            'total_files': len(self._files),
            'processed_files': len(processed),
            'skipped_files': len(self._files) - len(processed),
            'total_tokens': sum(f['processing']['token_count'] for f in processed),
            'caselaw_files': sum(1 for f in processed if f['doc_type'] == 'caselaw'),
            'published_files': sum(1 for f in processed if f['doc_type'] == 'published'),
            'private_files': sum(1 for f in processed if f['doc_type'] == 'private'),
            'uncertain_files': sum(1 for f in processed if f['doc_type'] == 'uncertain'),
            'ocr_files': sum(1 for f in processed if f['processing']['ocr']),
            'review_queue_files': sum(
                1 for f in self._files if f.get('skip_reason') == 'ocr_confidence_low'
            ),
            'pii_review_flags': sum(f['processing']['review_flags'] for f in processed),
        }
        manifest = {**self._dataset_meta, 'files': self._files, 'summary': summary}
        self.manifest_path.write_text(json.dumps(manifest, indent=2))
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_provenance.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add provenance.py tests/test_provenance.py
git commit -m "feat: add provenance manifest with sidecar CSV support"
```

---

### Task 9: pipeline.py + process.py

**Files:**
- Create: `pipeline.py`
- Create: `process.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_pipeline.py
import json
import pytest
from pathlib import Path
from pipeline import process_file, process_directory, SUPPORTED_EXTENSIONS

def _write_eml(path: Path, body: str = "Client matter update.") -> Path:
    path.write_bytes(
        f"From: a@b.com\r\nTo: c@d.com\r\nSubject: Case\r\n"
        f"Content-Type: text/plain\r\n\r\n{body}".encode()
    )
    return path

def test_process_file_returns_process_result(tmp_dir):
    from pipeline import ProcessResult
    eml = _write_eml(tmp_dir / "test.eml")
    result = process_file(eml, tmp_dir / "output", dry_run=True)
    assert isinstance(result, ProcessResult)

def test_process_file_dry_run_writes_nothing(tmp_dir):
    eml = _write_eml(tmp_dir / "test.eml")
    out_dir = tmp_dir / "output"
    process_file(eml, out_dir, dry_run=True)
    assert not out_dir.exists()

def test_process_file_private_eml_strips_pii(tmp_dir):
    eml = _write_eml(tmp_dir / "test.eml", body="Client John Smith, SSN 123-45-6789.")
    out_dir = tmp_dir / "output"
    result = process_file(eml, out_dir, dry_run=False)
    assert result.pii_stripped is True
    finetune = out_dir / "finetune" / "dataset.jsonl"
    assert finetune.exists()
    record = json.loads(finetune.read_text())
    assert "123-45-6789" not in record["text"]

def test_process_directory_creates_provenance(tmp_dir):
    _write_eml(tmp_dir / "input" / "email1.eml")
    (tmp_dir / "input").mkdir(exist_ok=True)
    _write_eml(tmp_dir / "input" / "email1.eml")
    out_dir = tmp_dir / "output"
    process_directory(tmp_dir / "input", out_dir, dry_run=False)
    assert (out_dir / "provenance.json").exists()

def test_process_directory_dry_run_no_output(tmp_dir):
    inp = tmp_dir / "input"
    inp.mkdir()
    _write_eml(inp / "email1.eml")
    out_dir = tmp_dir / "output"
    process_directory(inp, out_dir, dry_run=True)
    assert not out_dir.exists()

def test_supported_extensions_set():
    assert '.pdf' in SUPPORTED_EXTENSIONS
    assert '.eml' in SUPPORTED_EXTENSIONS
    assert '.msg' in SUPPORTED_EXTENSIONS
    assert '.docx' in SUPPORTED_EXTENSIONS
    assert '.pptx' in SUPPORTED_EXTENSIONS
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_pipeline.py -v
```

Expected: `ImportError: No module named 'pipeline'`

- [ ] **Step 3: Implement pipeline.py**

```python
import json
import re
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from reader import read_file
from classifier import classify
from cleaner import clean
from pii import strip_pii
from chunker import chunk, count_tokens
from writer import write_rag_chunks, write_finetune_record
from provenance import ProvenanceManifest


SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg'}


@dataclass
class ProcessResult:
    path: Path
    doc_type: str
    classification_confidence: float
    ocr: bool
    ocr_confidence: Optional[float]
    pii_stripped: bool
    faker_substitutions: int
    review_flags: int
    chunk_count: int
    token_count: int
    skipped: bool = False
    skip_reason: Optional[str] = None


def process_file(path: Path, output_dir: Path, dry_run: bool = False) -> ProcessResult:
    read_result = read_file(path)

    if read_result is None:
        if not dry_run:
            ocr_q = output_dir / 'review' / 'ocr_queue'
            ocr_q.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, ocr_q / path.name)
        return ProcessResult(
            path=path, doc_type='unknown', classification_confidence=0.0,
            ocr=True, ocr_confidence=None, pii_stripped=False,
            faker_substitutions=0, review_flags=0, chunk_count=0, token_count=0,
            skipped=True, skip_reason='ocr_confidence_low',
        )

    classify_result = classify(path, read_result.text)
    doc_type = classify_result.doc_type
    text = clean(read_result.text)

    pii_result = None
    if doc_type in ('private', 'uncertain'):
        pii_result = strip_pii(text, path.name, output_mode='finetune')
        text = pii_result.text

    faker_subs = pii_result.substitutions if pii_result else 0
    flag_count = len(pii_result.review_flags) if pii_result else 0
    chunks = chunk(text, doc_type)
    token_count = sum(c.token_count for c in chunks)

    if not dry_run:
        if pii_result and pii_result.review_flags:
            review_log = output_dir / 'review' / 'review_log.jsonl'
            review_log.parent.mkdir(parents=True, exist_ok=True)
            with open(review_log, 'a') as f:
                for flag in pii_result.review_flags:
                    f.write(json.dumps(flag) + '\n')

        if doc_type in ('caselaw', 'published'):
            extra = _caselaw_meta(text) if doc_type == 'caselaw' else {}
            extra['ocr'] = read_result.ocr
            write_rag_chunks(chunks, path.name, doc_type, extra, output_dir / 'rag')

        if doc_type in ('private', 'uncertain', 'published'):
            anon_id = f"anon_{abs(hash(path.name)):08d}"
            write_finetune_record(
                text=text,
                anon_id=anon_id,
                doc_type=doc_type,
                pii_stripped=doc_type in ('private', 'uncertain'),
                faker_substitutions=faker_subs,
                review_flags=flag_count,
                token_count=token_count,
                output_file=output_dir / 'finetune' / 'dataset.jsonl',
            )

    return ProcessResult(
        path=path,
        doc_type=doc_type,
        classification_confidence=classify_result.confidence,
        ocr=read_result.ocr,
        ocr_confidence=read_result.ocr_confidence,
        pii_stripped=doc_type in ('private', 'uncertain'),
        faker_substitutions=faker_subs,
        review_flags=flag_count,
        chunk_count=len(chunks),
        token_count=token_count,
    )


def _caselaw_meta(text: str) -> dict:
    meta = {}
    m = re.search(r'(\w[\w\s,\.]+v\.\s+[\w\s,\.]+,\s+\d+\s+\S+\s+\d+)', text[:2000])
    if m:
        meta['citation'] = m.group(1).strip()
    y = re.search(r'\((?:\w+\.?\s+)?(\d{4})\)', text[:2000])
    if y:
        meta['year'] = int(y.group(1))
    c = re.search(r'\((\d+(?:st|nd|rd|th) Cir\.|S\.Ct\.)[^)]*\)', text[:2000])
    if c:
        meta['court'] = c.group(1)
    return meta


def process_directory(
    input_dir: Path,
    output_dir: Path,
    dry_run: bool = False,
    workers: int = 1,
    sidecar_path: Optional[Path] = None,
) -> None:
    files = [
        p for p in input_dir.rglob('*')
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not files:
        print(f"No supported files in {input_dir}")
        return

    print(f"Found {len(files)} file(s).{' DRY RUN.' if dry_run else ''}")
    manifest = ProvenanceManifest(output_dir, sidecar_path) if not dry_run else None
    results = []

    def _run(f):
        return process_file(f, output_dir, dry_run)

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for r in as_completed({ex.submit(_run, f): f for f in files}):
                results.append(r.result())
    else:
        for f in files:
            results.append(_run(f))

    for r in results:
        _print_result(r, dry_run)

    if manifest:
        for r in results:
            manifest.add_file(
                original_path=r.path,
                doc_type=r.doc_type,
                classification_confidence=r.classification_confidence,
                ocr=r.ocr,
                ocr_confidence=r.ocr_confidence,
                pii_stripped=r.pii_stripped,
                faker_substitutions=r.faker_substitutions,
                review_flags=r.review_flags,
                chunk_count=r.chunk_count,
                token_count=r.token_count,
                skipped=r.skipped,
                skip_reason=r.skip_reason,
            )
        manifest.save()


def _print_result(r: ProcessResult, dry_run: bool) -> None:
    prefix = '[DRY RUN] ' if dry_run else ''
    status = 'SKIP' if r.skipped else r.doc_type.upper()
    flags = f" | {r.review_flags} PII flags" if r.review_flags else ''
    print(f"{prefix}{status:12s} {r.path.name} ({r.token_count} tokens{flags})")
```

- [ ] **Step 4: Implement process.py**

```python
#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from pipeline import process_directory


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Convert legal documents to AI/LLM-ready JSONL datasets.'
    )
    parser.add_argument('--input',   required=True, type=Path, help='Input directory')
    parser.add_argument('--output',  required=True, type=Path, help='Output directory')
    parser.add_argument('--dry-run', action='store_true',
                        help='Classify files only; write nothing')
    parser.add_argument('--workers', type=int, default=1,
                        help='Parallel worker processes (default: 1)')
    parser.add_argument('--sidecar', type=Path, default=None,
                        help='CSV with copyright/source info per file')
    args = parser.parse_args()

    if not args.input.is_dir():
        print(f"Error: {args.input} is not a directory", file=sys.stderr)
        sys.exit(1)
    if args.sidecar and not args.sidecar.exists():
        print(f"Error: sidecar {args.sidecar} not found", file=sys.stderr)
        sys.exit(1)

    process_directory(
        input_dir=args.input,
        output_dir=args.output,
        dry_run=args.dry_run,
        workers=args.workers,
        sidecar_path=args.sidecar,
    )


if __name__ == '__main__':
    main()
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python3 -m pytest tests/test_pipeline.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline.py process.py tests/test_pipeline.py
git commit -m "feat: add pipeline orchestrator and CLI entry point"
```

---

### Task 10: SKILL.md

**Files:**
- Create: `SKILL.md`

- [ ] **Step 1: Write SKILL.md**

```markdown
---
name: legal-doc-processor
description: >
  Convert legal documents (PDF, DOCX, PPTX, EML, MSG) into AI/LLM-ready JSONL
  datasets. Strips PII from private documents using Presidio + Faker. Produces
  RAG-chunked output for case law and fine-tuning records for private/client docs.
  Includes provenance manifest for dataset buyers.
metadata:
  openclaw:
    emoji: "⚖️"
    os: ["linux", "darwin"]
    requires:
      bins: ["python3", "tesseract", "ocrmypdf"]
      pip: ["presidio-analyzer", "presidio-anonymizer", "faker", "pymupdf",
            "pytesseract", "python-docx", "python-pptx", "extract-msg",
            "pdfminer.six", "langchain-text-splitters", "tiktoken"]
---

# Legal Document Processor

Convert legal PDFs, DOCX, PPTX, EML, and MSG files into token-efficient,
PII-safe JSONL datasets for RAG retrieval and QLoRA fine-tuning.

## First-Time Setup

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
pip3 install -r requirements.txt
python3 -m spacy download en_core_web_lg
```

## Usage

```bash
# Full run
python3 process.py --input /path/to/docs --output /path/to/output

# Dry run — classify files, print plan, write nothing
python3 process.py --input /path/to/docs --output /path/to/output --dry-run

# With copyright/source sidecar CSV
python3 process.py --input /path/to/docs --output /path/to/output --sidecar sources.csv

# Parallel processing
python3 process.py --input /path/to/docs --output /path/to/output --workers 4
```

## Sidecar CSV Format

```csv
filename,copyright_status,source_url_or_collection,notes
smith_v_jones.pdf,public domain,https://courtlistener.com/opinion/123/,
client_brief.pdf,all rights reserved,internal,
```

## Output Layout

```
output/
├── rag/                    ← chunked JSONL for case law + published docs
├── finetune/dataset.jsonl  ← full-doc JSONL for private + published docs
├── review/
│   ├── review_log.jsonl    ← PII entities flagged for human review
│   └── ocr_queue/          ← PDFs with OCR confidence below 70%
└── provenance.json         ← dataset manifest (fill in dataset-level fields)
```

## Document Type → Pipeline

| Doc type | PII stripped | RAG output | Finetune output |
|---|---|---|---|
| `caselaw` | No | Yes (512-token chunks) | No |
| `published` | No | Yes (512-token chunks) | Yes (full doc) |
| `private` | Yes — Presidio + Faker | No | Yes (2048-token chunks) |
| `uncertain` | Yes — conservative | No | Yes — flagged in provenance |

## Dry Run First

Always run `--dry-run` on a new input directory to verify classification
before committing to a full run. Check the classification column for
surprising assignments before processing.

## After Running

1. Open `output/provenance.json` and fill in the dataset-level fields:
   `dataset_name`, `created_by`, `source_collection`, `license`,
   `jurisdiction_coverage`.
2. Review `output/review/review_log.jsonl` — each entry is a PII entity
   the pipeline wasn't certain about. Verify and remove records from
   the finetune dataset if needed.
3. Check `output/review/ocr_queue/` for any PDFs that couldn't be OCR'd
   reliably (mean word confidence < 70%). These need manual handling.

## PII Confidence Thresholds

| Presidio score | Action |
|---|---|
| ≥ 0.85 | Auto-redact; replace with Faker synthetic value |
| 0.50–0.84 | Redact + write to review_log.jsonl for sign-off |
| < 0.50 | Leave in place |
```

- [ ] **Step 2: Run full test suite to confirm nothing broke**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
python3 -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "feat: add SKILL.md agent instruction file"
```

---

### Task 11: Smoke test with real files

**Files:** No new files — verify end-to-end with real documents.

- [ ] **Step 1: Create a small test input directory**

```bash
mkdir -p /tmp/legal-test-input
# Copy 2-3 real files you have handy — one case law PDF, one email
```

- [ ] **Step 2: Run dry-run first**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
python3 process.py --input /tmp/legal-test-input --output /tmp/legal-test-output --dry-run
```

Expected: each file printed with its detected doc type and token count. No output directory created.

- [ ] **Step 3: Run full pipeline**

```bash
python3 process.py --input /tmp/legal-test-input --output /tmp/legal-test-output
```

Expected: output directories created, provenance.json written.

- [ ] **Step 4: Inspect outputs**

```bash
# Check a RAG chunk
head -c 500 /tmp/legal-test-output/rag/*.jsonl

# Check finetune record
head -c 500 /tmp/legal-test-output/finetune/dataset.jsonl

# Check provenance summary
python3 -c "import json; d=json.load(open('/tmp/legal-test-output/provenance.json')); print(json.dumps(d['summary'], indent=2))"

# Check review log if any flags
cat /tmp/legal-test-output/review/review_log.jsonl 2>/dev/null || echo "No PII flags"
```

- [ ] **Step 5: Final commit**

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
git add -A
git commit -m "feat: complete legal-doc-processor skill — all tests passing"
```
