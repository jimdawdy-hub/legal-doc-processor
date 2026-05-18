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
      bins: ["python3.12", "tesseract", "ocrmypdf"]
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
python3.12 -m spacy download en_core_web_lg
```

## Usage

```bash
# Full run
python3.12 process.py --input /path/to/docs --output /path/to/output

# Dry run — classify files, print plan, write nothing
python3.12 process.py --input /path/to/docs --output /path/to/output --dry-run

# With copyright/source sidecar CSV
python3.12 process.py --input /path/to/docs --output /path/to/output --sidecar sources.csv

# Parallel processing
python3.12 process.py --input /path/to/docs --output /path/to/output --workers 4
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
4. Run re-identification risk assessment (optional, uses Claude API):
   ```bash
   python3.12 re_id_risk.py --output /path/to/output [--samples 20]
   ```
   This sends the highest-PII-flag records to `claude-opus-4-7` for
   adversarial re-identification analysis. Results are saved to
   `output/re_id_risk_report.json` and appended to `summary.html`.
   Requires `ANTHROPIC_API_KEY` in the environment.

## Interactive Review

```bash
# Start browser-based review server (replaces manual CSV handling)
python3.12 review_server.py --output /path/to/output

# OR: apply a decisions CSV directly from the command line
python3.12 apply_decisions.py --decisions decisions.csv --output /path/to/output --reviewer "Your Name"
```

## PII Confidence Thresholds

| Presidio score | Action |
|---|---|
| ≥ 0.85 | Auto-redact; replace with Faker synthetic value |
| 0.50–0.84 | Redact + write to review_log.jsonl for sign-off |
| < 0.50 | Leave in place |

## Running Tests

```bash
cd ~/.openclaw/workspace/skills/legal-doc-processor
python3.12 -m pytest tests/ -v
```

## Note on Python Version

Packages are installed under Python 3.12. Use `python3.12` explicitly,
not `python3` (which maps to 3.14 on this system).
