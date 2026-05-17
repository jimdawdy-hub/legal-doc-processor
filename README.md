# legal-doc-processor

An OpenClaw skill that converts legal documents into AI/LLM-ready datasets — token-efficient, PII-safe, and structured for both RAG retrieval and QLoRA fine-tuning.

## What It Does

Processes PDF, DOCX, PPTX, EML, and MSG files through a modular pipeline:

- **Auto-classifies** each document as `caselaw`, `published` (journals, CLEs, treatises), `private` (emails, pleadings, client notes), or `uncertain`
- **Strips PII** from private documents using Microsoft Presidio for detection and Faker for realistic synthetic replacement — so fine-tuning data reads as natural legal text, not redacted fragments
- **Produces RAG-ready JSONL** for case law and published sources (512-token chunks with citation metadata)
- **Produces fine-tuning JSONL** for private documents (2048-token records, anonymized source IDs)
- **Generates a provenance manifest** suitable for dataset licensing and sale — includes SHA-256 hashes of source files, processing metadata, and fields for copyright/source attribution
- **OCR support** for scanned PDFs via tesseract/ocrmypdf, with a quality gate that routes low-confidence files to a review queue rather than polluting the dataset

## Quick Start

```bash
# Install dependencies
pip3 install -r requirements.txt
python3.12 -m spacy download en_core_web_lg

# Dry run — classify files, print plan, write nothing
python3.12 process.py --input /path/to/docs --output /path/to/output --dry-run

# Full run
python3.12 process.py --input /path/to/docs --output /path/to/output

# With copyright/source sidecar CSV
python3.12 process.py --input /path/to/docs --output /path/to/output --sidecar sources.csv

# Parallel processing
python3.12 process.py --input /path/to/docs --output /path/to/output --workers 4
```

> **Note:** On systems where `python3` maps to Python 3.14+, use `python3.12` explicitly. Dependencies are installed under 3.12.

## Document Type → Pipeline

| Doc type | PII stripped | RAG output | Finetune output |
|---|---|---|---|
| `caselaw` | No | Yes — 512-token chunks with citation metadata | No |
| `published` | No | Yes — 512-token chunks | Yes — full doc |
| `private` | Yes — Presidio + Faker | No | Yes — 2048-token chunks |
| `uncertain` | Yes — conservative | No | Yes — flagged in provenance |

## Output Layout

```
output/
├── rag/                    ← one JSONL per source document, one record per chunk
├── finetune/
│   └── dataset.jsonl       ← one record per document (appended across all runs)
├── review/
│   ├── review_log.jsonl    ← PII entities scored 0.50–0.84 (human sign-off needed)
│   └── ocr_queue/          ← scanned PDFs where OCR confidence fell below 70%
└── provenance.json         ← dataset manifest
```

### RAG chunk record

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

### Finetune record

```json
{
  "text": "Michael Johnson filed a motion on March 3rd...",
  "metadata": {
    "source": "anon_00042381",
    "doc_type": "private",
    "pii_stripped": true,
    "faker_substitutions": 7,
    "review_flags": 2,
    "token_count": 1204
  }
}
```

## Sidecar CSV

Pre-populate copyright and source fields in the provenance manifest without editing JSON:

```csv
filename,copyright_status,source_url_or_collection,notes
smith_v_jones.pdf,public domain,https://courtlistener.com/opinion/123/,
client_brief.pdf,all rights reserved,internal,
```

Pass with `--sidecar sources.csv`. Unmatched files leave those fields blank.

## PII Handling

Detection uses [Microsoft Presidio](https://github.com/microsoft/presidio) with a supplemental `PatternRecognizer` for high-reliability SSN detection. Replacement uses [Faker](https://faker.readthedocs.io/) for finetune output (natural text) and typed tokens for RAG output.

| Presidio confidence | Action |
|---|---|
| ≥ 0.85 | Auto-redact; replace with Faker synthetic value |
| 0.50 – 0.84 | Redact + write to `review_log.jsonl` for human sign-off |
| < 0.50 | Leave in place |

Faker replacements are **consistent within a document** — the same detected entity always maps to the same synthetic value, preserving co-reference coherence in the training data.

Entity types detected: `PERSON`, `PHONE_NUMBER`, `EMAIL_ADDRESS`, `LOCATION`, `US_SSN`, `DATE_TIME` (birthdate context), `US_BANK_NUMBER`, `CREDIT_CARD`, `US_PASSPORT`, `US_DRIVER_LICENSE`, `IP_ADDRESS`, `MEDICAL_LICENSE`

## Classification Signals

Rule-based, no ML model required. Scored against the first 5,000 characters of each document.

**Extension shortcuts (high confidence):**
- `.eml`, `.msg` → `private` (0.99)
- `.pptx` → `published` (0.90)

**Content signals (partial list):**
- Citation patterns (`123 F.3d 456`, `U.S.`, `S.Ct.`) → caselaw
- `OPINION`, `AFFIRMED`, `REVERSED` → caselaw
- `Vol. 42, No. 3`, `ISSN`, `CLE` → published
- `To:/From:/Subject:` headers → private
- `ATTORNEYS FOR`, `LAW DIVISION` → private
- `PLAINTIFF`, `DEFENDANT` → private

## After Running

1. Fill in `output/provenance.json` dataset-level fields: `dataset_name`, `created_by`, `source_collection`, `license`, `jurisdiction_coverage`
2. Review `output/review/review_log.jsonl` — verify each flagged PII entity
3. Check `output/review/ocr_queue/` for PDFs needing manual OCR handling

## File Structure

```
legal-doc-processor/
├── process.py          ← CLI entry point
├── pipeline.py         ← orchestrator
├── reader.py           ← PDF/DOCX/PPTX/EML/MSG → ReadResult
├── classifier.py       ← text + path → ClassifyResult (doc_type, confidence)
├── cleaner.py          ← strip headers, line numbers, Westlaw annotations
├── pii.py              ← Presidio detection + Faker replacement + review log
├── chunker.py          ← token-aware chunking via langchain-text-splitters
├── writer.py           ← RAG JSONL + finetune JSONL writers
├── provenance.py       ← ProvenanceManifest with sidecar CSV support
├── SKILL.md            ← OpenClaw agent instructions
├── requirements.txt
└── tests/              ← 56 tests, pytest
```

## Running Tests

```bash
python3.12 -m pytest tests/ -v
```

## Requirements

**System:** Python 3.12+, tesseract, ocrmypdf

**Python packages:** see `requirements.txt`

**spaCy model:** `python3.12 -m spacy download en_core_web_lg`

## License

MIT
