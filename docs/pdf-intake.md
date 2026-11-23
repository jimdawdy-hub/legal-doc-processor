# PDF intake and metadata

## Text used for datasets

`process.py` checks PDFs with PyMuPDF. If any page draws an image or contains invisible text, or if the PDF has optional content layers, the reader runs local OCR on the whole document. The reader passes rendered page pixels to Tesseract, including visible annotations. The original embedded text stays out of the dataset even when a PDF already has a searchable layer.

PDFs without those indicators use PDFMiner when it extracts at least 100 characters after trimming leading and trailing whitespace. Shorter results go through local OCR. The OCR quality check samples up to the first three pages and requires a mean word confidence of at least 70%. The pipeline skips dataset output if confidence is unavailable or below 70%, or if OCR fails or returns no text. During a normal run, it copies the source to the private `review/ocr_queue/`.

## Before AI assessment

`re_id_risk.py` sends text records. Its assessment and retry commands match every selected record to a source in `provenance.json`. For a PDF source, the entry must have `processing.pdf_text_checked: true`. If an older batch lacks that field, run `process.py` again from the source files into a new output directory.

Keep `provenance.json` and `review/ocr_queue/` private. Dataset records use opaque source IDs. Provenance retains the original filename, full path, and SHA-256 file hash for local review. The dataset pipeline leaves the source PDF and its metadata intact.

## Finalizing a PDF copy

Use `strip_pdf_text.py` on a scanned copy whose page images already contain any intended visible redactions. The utility preserves the page images and removes all page text objects, page annotations, embedded files, standard document properties such as title and author, and the XMP packet (embedded XML metadata).

```bash
python strip_pdf_text.py visually-cleaned.pdf image-only.pdf
python strip_pdf_text.py visually-cleaned.pdf searchable.pdf --reocr
```

The first command verifies that no extractable page text remains. The second rebuilds a searchable layer from the remaining page content. Review the output images before sharing either copy. Visible patient information, including handwriting, stays in the copy and can appear in search when OCR recognizes it.
