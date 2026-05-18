# legal-doc-processor

An OpenClaw skill that converts legal documents into AI/LLM-ready datasets — token-efficient, PII-safe, and structured for both RAG retrieval and QLoRA fine-tuning.

After processing, an AI red-team pass sends every private document through an adversarial re-identification analysis using Claude, identifying quasi-identifiers that survived the first PII pass and applying a targeted second-pass redaction automatically.

## What It Does

Processes PDF, DOCX, PPTX, EML, and MSG files through a modular pipeline:

- **Auto-classifies** each document as `caselaw`, `published` (journals, CLEs, treatises), `private` (emails, pleadings, client notes), or `uncertain`
- **Strips PII** from private documents using Microsoft Presidio for detection and Faker for realistic synthetic replacement — so fine-tuning data reads as natural legal text, not redacted fragments
- **Produces RAG-ready JSONL** for case law and published sources (512-token chunks with citation metadata)
- **Produces fine-tuning JSONL** for private documents (2048-token records, anonymized source IDs)
- **Generates a provenance manifest** suitable for dataset licensing and sale — includes SHA-256 hashes of source files, processing metadata, and fields for copyright/source attribution
- **OCR support** for scanned PDFs via tesseract/ocrmypdf, with a quality gate that routes low-confidence files to a review queue rather than polluting the dataset
- **AI red-team verification** sends every fine-tuning record through an adversarial re-identification assessment and applies a second-pass redaction to catch what Presidio missed

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

## AI Red-Team Verification

Presidio catches common PII (names, SSNs, phone numbers, dates) but misses **quasi-identifiers** — combinations of retained facts that can still identify a person even without an explicit name. In legal documents this typically means case numbers, uncommon surnames, specific medical facilities, treating physicians, docket entries, and narrow clinical narratives.

After the initial pipeline run, `re_id_risk.py` sends every fine-tuning record to **Claude (`claude-opus-4-7`)** with an adversarial framing: *"You are a motivated adversary with access to PACER, CourtListener, newspaper archives, and medical provider directories. Can you identify the subject of this document?"*

For each record Claude returns:

- **Risk level** — `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`
- **Quasi-identifiers** — specific text still present that narrows identity (e.g. case number, rare surname, facility + procedure + date combination)
- **Reconstruction path** — step-by-step: how an adversary would actually find this person
- **Additional redactions** — exact phrases that should be removed

`second_pass.py` then compiles all flagged terms into a regex pattern set and applies them across every fine-tuning and RAG JSONL file, replacing each with a typed token (`[PERSON]`, `[CASE_NO]`, `[ORGANIZATION]`, `[ADDRESS]`, etc.). Results are recorded in the provenance manifest and appended to the interactive HTML report.

### Running the red-team pass

```bash
# Assess all records and immediately apply second-pass redaction (recommended)
ANTHROPIC_API_KEY=sk-ant-... python3.12 re_id_risk.py --output /path/to/output --apply

# Assess only, review the report, then apply separately
ANTHROPIC_API_KEY=sk-ant-... python3.12 re_id_risk.py --output /path/to/output
python3.12 second_pass.py --output /path/to/output --dry-run   # preview first
python3.12 second_pass.py --output /path/to/output

# Retry any records that returned PARSE_ERROR (large docs sometimes need a retry)
ANTHROPIC_API_KEY=sk-ant-... python3.12 re_id_risk.py --output /path/to/output --retry-errors --apply

# Add case-specific terms to always redact (one per line, optional [TOKEN] in col 2)
python3.12 second_pass.py --output /path/to/output --blocklist custom_terms.txt
```

Requires an [Anthropic API key](https://console.anthropic.com) (separate from a Claude.ai subscription). At `claude-opus-4-7` pricing (~$5/1M input, $25/1M output), a 49-record dataset costs roughly $3–4 for the full assessment.

### What the red-team catches

In testing on a real personal-injury malpractice case folder (60 documents, 49 fine-tuning records):

- Presidio's first pass generated **7,145 PII flags** across the dataset
- The red-team pass identified **18 of 20 sampled records** as `HIGH` or `CRITICAL` risk despite that redaction — primarily because Presidio never learned the case-specific surname, missed case numbers entirely, and left treating physician and facility names intact
- The second-pass redaction applied an additional **15,000+ substitutions** across the dataset using the red-team's findings

The most common failure modes the red-team catches:

| What Presidio misses | Why | Red-team action |
|---|---|---|
| Uncommon surnames | Not in spaCy's NER training data | Flags as `patient_surname` → `[PERSON]` |
| Court case numbers | Not a standard PII type | Flags as `case_number` → `[CASE_NO]` |
| Treating physicians | Presidio only catches detected names, not OCR-mangled headers | Flags as `treating_physician` → `[PERSON]` |
| Named facilities | Organizations aren't PHI under HIPAA Safe Harbor | Flags as `facility` → `[ORGANIZATION]` |
| Quasi-identifier combinations | No single field is PII, but together they identify | Narrative reconstruction analysis |

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
├── rag/                      ← one JSONL per source document, one record per chunk
├── finetune/
│   └── dataset.jsonl         ← one record per document (appended across all runs)
├── review/
│   ├── review_log.jsonl      ← PII entities scored 0.50–0.84 (human sign-off needed)
│   └── ocr_queue/            ← scanned PDFs where OCR confidence fell below 70%
├── re_id_risk_report.json    ← per-record red-team assessments + overall risk summary
├── summary.html              ← interactive report (PII flags, decisions, risk table)
└── provenance.json           ← dataset manifest with full audit trail
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

Faker replacements are **consistent within a document** — the same detected entity always maps to the same synthetic value, preserving co-reference coherence in the training data. Date shifts are format-preserving and consistent per document (180–730 day offset seeded by content hash), so temporal relationships between dates are preserved.

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

## Interactive Review

After each pipeline run, `summary.html` opens in any browser. From there you can review every flagged PII entity, batch-approve or restore by file or entity type, and submit decisions directly to the local review server.

```bash
# Start local review server (browser opens automatically)
python3.12 review_server.py --output /path/to/output

# OR apply a decisions CSV from the command line
python3.12 apply_decisions.py --decisions decisions.csv --output /path/to/output --reviewer "Your Name"
```

The review server writes an audit entry to `provenance.json` and archives each decisions file to `source_dir/audit/` for the chain-of-custody record.

## After Running

1. Fill in `output/provenance.json` dataset-level fields: `dataset_name`, `created_by`, `source_collection`, `license`, `jurisdiction_coverage`
2. Review `output/review/review_log.jsonl` — verify each flagged PII entity
3. Check `output/review/ocr_queue/` for PDFs needing manual OCR handling
4. Run the AI red-team verification: `ANTHROPIC_API_KEY=... python3.12 re_id_risk.py --output /path/to/output --apply`

## File Structure

```
legal-doc-processor/
├── process.py          ← CLI entry point
├── pipeline.py         ← orchestrator
├── reader.py           ← PDF/DOCX/PPTX/EML/MSG/TXT → ReadResult
├── classifier.py       ← text + path → ClassifyResult (doc_type, confidence)
├── cleaner.py          ← strip headers, line numbers, Westlaw annotations
├── pii.py              ← Presidio detection + Faker replacement + review log
├── chunker.py          ← token-aware chunking via langchain-text-splitters
├── writer.py           ← RAG JSONL + finetune JSONL writers
├── provenance.py       ← ProvenanceManifest with sidecar CSV support
├── reporter.py         ← generates summary.html + review_log.csv
├── review_server.py    ← local HTTP server for in-browser PII review
├── apply_decisions.py  ← apply decisions CSV, write audit entry
├── review_pii.py       ← standalone CLI for reviewing PII flags
├── re_id_risk.py       ← AI red-team: adversarial re-identification assessment
├── second_pass.py      ← targeted second-pass redaction from red-team findings
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

**For AI red-team:** Anthropic API key from [console.anthropic.com](https://console.anthropic.com)

## License

MIT
