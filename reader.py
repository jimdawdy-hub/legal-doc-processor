import csv
import email as email_lib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from utils import SUPPORTED_EXTENSIONS

from pdfminer.high_level import extract_text as pdfminer_extract

try:
    import fitz  # pymupdf
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

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


def read_file(path: Path) -> Optional['ReadResult']:
    """
    Returns ReadResult, or None if a PDF cannot be safely read
    (caller should move it to the local ocr_queue).
    """
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported format: {ext}")
    size = path.stat().st_size

    if ext == '.txt':
        return _read_txt(path, size)
    elif ext == '.pdf':
        return _read_pdf(path, size)
    elif ext == '.docx':
        return _read_docx(path, size)
    elif ext == '.pptx':
        return _read_pptx(path, size)
    elif ext == '.eml':
        return _read_eml(path, size)
    elif ext == '.msg':
        return _read_msg(path, size)


def _read_txt(path: Path, size: int) -> ReadResult:
    text = path.read_text(encoding='utf-8', errors='replace')
    return ReadResult(
        text=text, filename=path.name,
        extension='.txt', size_bytes=size, ocr=False,
    )


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
    try:
        return ReadResult(
            text=f"{headers}\n\n{msg.body or ''}".strip(),
            filename=path.name, extension='.msg', size_bytes=size,
        )
    finally:
        msg.close()


def _read_pdf(path: Path, size: int) -> Optional[ReadResult]:
    if not FITZ_AVAILABLE:
        return None

    with fitz.open(str(path)) as doc:
        # A scanned PDF's embedded text may predate changes to its visible pages.
        if doc.get_ocgs() or any(
            any(kind in ('fill-image', 'ignore-text') for kind, _ in page.get_bboxlog())
            or any(span['opacity'] == 0 for span in page.get_texttrace())
            for page in doc
        ):
            return _ocr_pdf(doc, path, size)

        text = pdfminer_extract(str(path)) or ''
        if len(text.strip()) >= 100:
            return ReadResult(
                text=text, filename=path.name,
                extension='.pdf', size_bytes=size, ocr=False,
            )
        return _ocr_pdf(doc, path, size)


def _ocr_pdf(doc, path: Path, size: int) -> Optional[ReadResult]:
    try:
        confidence = _sample_ocr_confidence(doc)
        if confidence is None or confidence < 70.0:
            return None
        text = '\n\n'.join(
            _ocr_page(page, 300).strip()
            for page in doc
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        return None
    if not text.strip():
        return None
    return ReadResult(
        text=text, filename=path.name, extension='.pdf',
        size_bytes=size, ocr=True, ocr_confidence=confidence,
    )


def _ocr_page(page, dpi: int, *config: str) -> str:
    # Render visible annotations too so a displayed redaction still covers the page.
    pix = page.get_pixmap(dpi=dpi, annots=True)
    output = subprocess.run(
        ['tesseract', 'stdin', 'stdout', '--dpi', str(dpi), *config],
        input=pix.tobytes('png'), capture_output=True, check=True,
    )
    return output.stdout.decode('utf-8')


def _sample_ocr_confidence(doc) -> Optional[float]:
    """Return mean word confidence for the first three rendered pages."""
    confidences = []
    for page_num in range(min(3, len(doc))):
        data = _ocr_page(doc[page_num], 200, 'tsv')
        rows = csv.DictReader(data.splitlines(), delimiter='\t', quoting=csv.QUOTE_NONE)
        for row in rows:
            try:
                value = float(row.get('conf'))
            except (ValueError, TypeError):
                continue
            if 0 < value <= 100:
                confidences.append(value)
    return sum(confidences) / len(confidences) if confidences else None
