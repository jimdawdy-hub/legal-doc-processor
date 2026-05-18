# Legal Document Processor — Design Spec
**Date:** 2026-05-17  
**Status:** Approved  
**Location:** `~/.openclaw/workspace/skills/legal-doc-processor/`

---

## Purpose

Convert large collections of legal documents (PDFs, DOCX, PPTX, EML, MSG) into AI/LLM-ready datasets optimized for:
1. Minimal token usage
2. Local and frontier model compatibility
3. RAG/vector database retrieval (case law)
4. Fine-tuning via QLoRA (private/client documents)

PII stripping is applied to all private documents. Published sources (case law, journals, treatises) are not stripped.

---

## Architecture

Modular pipeline. Each stage has one job and a clean interface. The CLI orchestrates them in order.

```
legal-doc-processor/
├── SKILL.md
├── requirements.txt
├── process.py          ← CLI entry point
├── pipeline.py         ← orchestrator
├── reader.py           ← all formats → (text, file_metadata)
├── classifier.py       ← auto-detect doc type + confidence score
├── cleaner.py          ← strip noise (headers, line numbers, whitespace)
├── pii.py              ← Presidio detection + Faker replacement + review log
├── chunker.py          ← token-aware chunking via langchain-text-splitters
├── writer.py           ← JSONL output (RAG and/or finetune)
└── provenance.py       ← per-file and dataset-level manifest
```

---

## CLI

```bash
python process.py --input /path/to/docs --output /path/to/output
python process.py --input /path/to/docs --output /path/to/output --dry-run
python process.py --input /path/to/docs --output /path/to/output --workers 4
python process.py --input /path/to/docs --output /path/to/output --sidecar sources.csv
```

- `--dry-run` — classify all files, print what pipeline would run, write nothing
- `--workers N` — parallel processing for large batches
- `--sidecar <csv>` — pre-populate `copyright_status` and `source_url_or_collection` in provenance from a CSV (columns: `filename`, `copyright_status`, `source_url_or_collection`, `notes`); optional

---

## Output Directory Layout

```
output/
├── rag/                    ← chunked JSONL (case law + published docs)
├── finetune/               ← full-doc JSONL (private + published docs)
├── review/
│   ├── review_log.jsonl    ← uncertain PII entities for human sign-off
│   └── ocr_queue/          ← PDFs with OCR confidence below threshold
└── provenance.json         ← dataset manifest
```

---

## Stage 1: Reading (`reader.py`)

Normalizes all formats to `(text, file_metadata)`.

| Format | Library | Notes |
|---|---|---|
| `.pdf` (text) | `pdfminer.six` | Primary; layout-preserving text extraction |
| `.pdf` (scanned) | `ocrmypdf` → `pdfminer.six` | Triggered when extracted text < 100 chars |
| `.docx` | `python-docx` | Paragraphs, tables, headers/footers in reading order |
| `.pptx` | `python-pptx` | Slides + speaker notes with slide boundary markers |
| `.eml` | stdlib `email` | Plain text body; To/From/Subject/Date as metadata |
| `.msg` | `extract-msg` | Same as EML treatment |

**OCR quality gate** (when pdfminer yields < 100 chars from non-empty PDF):
1. Run `ocrmypdf --output-type pdf --redo-ocr`
2. Compute mean word confidence from tesseract hOCR output
3. Mean ≥ 70% → use OCR text, set `ocr: true` in provenance
4. Mean < 70% → copy to `output/review/ocr_queue/`, skip from dataset, log reason

---

## Stage 2: Classification (`classifier.py`)

Auto-detects document type using rule-based scoring. No ML classifier.

**Output:** one of `caselaw`, `published`, `private`, `uncertain`

**Extension shortcuts (high confidence, no content scan):**
- `.eml`, `.msg` → `private` (0.99)
- `.pptx` → `published` (0.90)

**Content signals:**

| Signal | Type | Weight |
|---|---|---|
| Citation pattern (`123 F.3d 456`, `U.S.`, `S.Ct.`, `N.E.2d`, etc.) | caselaw | +0.40 |
| Keywords: OPINION, HELD, AFFIRMED, REVERSED, JUDGMENT | caselaw | +0.30 |
| Westlaw/Lexis header patterns | caselaw | +0.20 |
| Byline, volume/issue, ISSN/ISBN, journal name | published | +0.30 |
| "CLE", "Continuing Legal Education", "© [Publisher]" | published | +0.30 |
| "PLAINTIFF", "DEFENDANT", "IN THE MATTER OF", docket number (no citation) | private | +0.35 |
| Email headers in body: `To:`, `From:`, `Subject:`, `CC:`, `BCC:` | private | +0.40 |
| Salutation/closing ("Dear [Name]", "Sincerely", "Best regards") | private | +0.30 |
| "ATTORNEY FOR", "ATTORNEYS FOR" | private | +0.35 |
| "LAW DIVISION" | private | +0.25 |
| "CASE NOTE", "CLIENT", "CONFIDENTIAL" | private | +0.25 |

Scores normalized per type. Winning type with score ≥ 0.60 is assigned. Score < 0.60 → `uncertain`, treated as `private` for PII purposes, flagged to review log.

---

## Stage 3: Cleaning (`cleaner.py`)

Runs on all doc types after reading, before PII or chunking.

- Collapse 3+ blank lines to 2
- Strip repeated page headers/footers (lines appearing at consistent positions across pages)
- Remove leading line numbers (`^\d+\s` — common in pleadings and transcripts)
- Normalize whitespace and smart quotes
- Remove Westlaw/Lexis cost/annotation lines and watermark text

---

## Stage 4: PII Stripping (`pii.py`)

**Runs on `private` documents only.** Published sources are not stripped.

**Libraries:** `presidio-analyzer` (detection) + `presidio-anonymizer` + `Faker` (replacement)

**Entity types detected:**
`PERSON`, `PHONE_NUMBER`, `EMAIL_ADDRESS`, `LOCATION`, `US_SSN`, `DATE_TIME` (birthdate context), `US_BANK_NUMBER`, `CREDIT_CARD`, `US_PASSPORT`, `US_DRIVER_LICENSE`, `IP_ADDRESS`, `MEDICAL_LICENSE`

**Confidence thresholds:**

| Score | Action |
|---|---|
| ≥ 0.85 | Auto-redact; replace with Faker synthetic equivalent |
| 0.50 – 0.84 | Redact conservatively; write to review log for human sign-off |
| < 0.50 | Leave in place |

**Replacement strategy:**
- **Finetune output:** Faker generates type-matched synthetic data (fake name, fake phone, fake address). Replacements are consistent within a document — the same detected entity always maps to the same fake value via an in-memory substitution table per document. This preserves co-reference coherence.
- **RAG output:** Typed tokens (`[PERSON]`, `[PHONE]`, `[ADDRESS]`, etc.) — precision over readability.

**Review log entry:**
```json
{
  "file": "brief_001.pdf",
  "entity_type": "PERSON",
  "original_text": "Hon. Robert Chen",
  "confidence": 0.71,
  "context": "...filed before [PERSON] in the Law Division...",
  "action": "redacted_pending_review"
}
```

Context window is ±60 characters around the entity — enough to judge client name vs. judge name without exposing the full document.

---

## Stage 5: Chunking (`chunker.py`)

Uses `langchain-text-splitters` with `tiktoken` for token counting.

| Doc type | Chunk size | Overlap | Split priority |
|---|---|---|---|
| `caselaw` | 512 tokens | 64 tokens | Section headers → paragraphs → sentences |
| `published` | 512 tokens | 64 tokens | Paragraphs → sentences |
| `private` (finetune) | 2048 tokens | 128 tokens | Only for very long docs; most go out as single records |

**Rationale:** 512 tokens matches embedding model limits and retrieval precision needs for RAG. 2048 tokens captures most complete private documents whole, matching typical QLoRA context window sizes.

---

## Stage 6: Output (`writer.py`)

**RAG output** — `output/rag/` — one JSONL per source document:
```json
{
  "id": "smith_v_jones_pdf_chunk_004",
  "text": "The court held that...",
  "metadata": {
    "source": "smith_v_jones.pdf",
    "doc_type": "caselaw",
    "citation": "Smith v. Jones, 123 F.3d 456 (7th Cir. 2019)",
    "court": "7th Cir.",
    "year": 2019,
    "chunk_index": 4,
    "total_chunks": 17,
    "token_count": 498,
    "ocr": false
  }
}
```

**Finetune output** — `output/finetune/` — one JSONL per run:
```json
{
  "text": "Michael Johnson filed a motion on March 3rd...",
  "metadata": {
    "source": "anon_0042",
    "doc_type": "private",
    "pii_stripped": true,
    "faker_substitutions": 7,
    "review_flags": 2,
    "token_count": 1204
  }
}
```

`source` in finetune records is an anonymized ID — the original filename lives only in `provenance.json`.

---

## Stage 7: Provenance (`provenance.py`)

Writes `output/provenance.json` at end of each run.

**Sidecar CSV** (`--sidecar sources.csv`) pre-populates `copyright_status` and `source_url_or_collection` per file, eliminating manual entry in the provenance JSON. Format:

```csv
filename,copyright_status,source_url_or_collection,notes
smith_v_jones.pdf,public domain,https://courtlistener.com/opinion/123/,
client_brief_001.pdf,all rights reserved,internal,
```

Matching is by filename only (no path). Unmatched files leave those fields blank as before. The CSV is optional — pipeline runs normally without it.

**Dataset-level fields (written once as template; not overwritten on re-runs):**
```json
{
  "dataset_name": "",
  "version": "1.0.0",
  "created": "2026-05-17",
  "created_by": "",
  "source_collection": "",
  "license": "",
  "jurisdiction_coverage": [],
  "notes": ""
}
```

**Per-file record (auto-populated):**
```json
{
  "original_filename": "smith_v_jones.pdf",
  "sha256": "a3f9c2...",
  "doc_type": "caselaw",
  "classification_confidence": 0.91,
  "date_processed": "2026-05-17T14:32:01Z",
  "copyright_status": "",
  "source_url_or_collection": "",
  "processing": {
    "ocr": false,
    "ocr_confidence": null,
    "pii_stripped": false,
    "faker_substitutions": 0,
    "review_flags": 0,
    "chunk_count": 14,
    "token_count": 6842
  }
}
```

`sha256` is computed from the original source file before any processing — buyers can verify dataset integrity against source documents.

**Summary block (auto-appended):**
```json
"summary": {
  "total_files": 214,
  "total_tokens": 3847291,
  "caselaw_files": 180,
  "published_files": 22,
  "private_files": 12,
  "ocr_files": 31,
  "review_queue_files": 4,
  "pii_review_flags": 17
}
```

---

## Dependencies

**Already installed:** `pdfminer.six`, `extract-msg`, `python-pptx`, `tiktoken`, `langchain-text-splitters`, `tesseract`, `ocrmypdf`, `sentence-transformers`

**To install:**
```
presidio-analyzer
presidio-anonymizer
spacy
en_core_web_lg  (spaCy model, via: python -m spacy download en_core_web_lg)
faker
pymupdf         (faster PDF fallback; optional but recommended)
python-docx
```

---

## Doc Type → Pipeline Matrix

| Doc type | PII strip | RAG output | Finetune output |
|---|---|---|---|
| `caselaw` | No | Yes (512-token chunks) | No |
| `published` | No | Yes (512-token chunks) | Yes (full doc) |
| `private` | Yes (Presidio + Faker) | No | Yes (2048-token chunks) |
| `uncertain` | Yes (conservative) | No | Yes, flagged |
