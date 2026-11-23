#!/usr/bin/env python3
"""
Remove the hidden text layer from a scanned PDF.

Anonymizing page images does not touch the OCR text behind them: the original
names, dates and IDs stay selectable and searchable in the PDF. This tool deletes
that text layer so only the page images remain, optionally re-OCRing the
(already cleaned) images so the output stays searchable.

Usage:
    python3.12 strip_pdf_text.py INPUT.pdf OUTPUT.pdf [--reocr] [--dpi 300]

--reocr  Rebuild an invisible text layer by OCRing the page images in INPUT.
         Only use this on a PDF whose images are already anonymized; the new
         text is whatever is visible in the images.

Also clears the document metadata and XMP packet, and removes annotations and
embedded files. The input is never modified; OUTPUT must be a different path.
"""

import argparse
import os
import sys
from pathlib import Path

import fitz  # pymupdf


def strip_text_layer(src: Path, dst: Path, reocr: bool = False, dpi: int = 300) -> dict:
    """Write dst = src minus all text. Returns {'pages': n, 'chars_removed': n, 'chars_left': n}."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if src == dst:
        raise ValueError("output must differ from input")
    if reocr:
        import pytesseract  # imported lazily: only needed for --reocr
        from PIL import Image
        import io

    removed = 0
    with fitz.open(str(src)) as doc:
        if doc.needs_pass:
            raise ValueError("encrypted PDF")
        for page in doc:
            removed += len(page.get_text().strip())
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
            # Redact the whole page: removes text objects, leaves images and line art.
            page.add_redact_annot(page.rect)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        if reocr:
            for page in doc:
                pix = page.get_pixmap(dpi=dpi)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                layer = pytesseract.image_to_pdf_or_hocr(img, extension="pdf")
                with fitz.open("pdf", layer) as ocr_doc:
                    # Overlay the OCR page (same raster + invisible text) on ours.
                    page.show_pdf_page(page.rect, ocr_doc, 0, overlay=True)
        doc.set_metadata({})
        doc.del_xml_metadata()
        for name in list(doc.embfile_names()):
            doc.embfile_del(name)
        tmp = dst.with_name(dst.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        doc.save(str(tmp), garbage=4, deflate=True)
        pages = doc.page_count
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)

    with fitz.open(str(dst)) as out:
        left = sum(len(p.get_text().strip()) for p in out)
    if not reocr and left:
        dst.unlink()
        raise RuntimeError(f"{left} characters of text survived stripping")
    return {"pages": pages, "chars_removed": removed, "chars_left": left}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--reocr", action="store_true")
    ap.add_argument("--dpi", type=int, default=300)
    a = ap.parse_args(argv)
    r = strip_text_layer(a.input, a.output, reocr=a.reocr, dpi=a.dpi)
    print(f"{a.output}: {r['pages']} pages, removed {r['chars_removed']} chars, "
          f"{r['chars_left']} chars in new layer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
