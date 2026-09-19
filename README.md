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
│   └── dataset.jsonl         ← one record per document, replaced on re-run
├── review/
│   ├── review_log.csv        ← identifier types and counts (no values)
│   ├── ocr_queue/            ← scanned PDFs where OCR confidence fell below 70%
│   └── pii_queue/            ← documents held back rather than emitted
├── re_id_risk_report.json    ← per-record red-team assessments + overall risk summary
├── summary.html              ← read-only run report
└── provenance.json           ← dataset manifest with full audit trail
```

**Nothing in this directory carries an identifier value, a context excerpt, or
a source filename.** It is the one thing that leaves the machine, so it is the
trust boundary. Documents appear there only under an anonymous id.

The redaction records live **outside** it, under
`~/.local/share/legal-doc-processor/` (override with `LEGAL_DOC_RECORDS_DIR`),
as two artifacts with different lifetimes:

```
evidence/<doc_key>.json                  ← identifier types, counts, locations,
                                            the id→filename mapping. No values.
                                            Kept indefinitely; losing it
                                            discloses nothing.
verification/<batch_id>/<doc_key>.json   ← the original values, so a human can
                                            check the batch. Deleted when the
                                            batch is accepted.
```

Directories are `0700` and files `0600`. Accept a batch once you have checked
it — that is what deletes the values:

```bash
python3.12 review_pii.py --list                  # batches still holding values
python3.12 review_pii.py --batch <id> --summary  # what was found
python3.12 review_pii.py --accept <id>           # delete the values
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
| ≥ 0.85 | Redact; replace with a type-correct synthetic value |
| 0.30 – 0.84 | Redact, and count as an ambiguous detection in the record |
| < 0.30 | Leave in place, but still recorded |

The floor is 0.30 rather than 0.50 because missing an identifier is the
expensive failure: over-redaction costs a consistently substituted fake value,
under-redaction costs a disclosure.

Faker replacements are **consistent within a document** — the same detected entity always maps to the same synthetic value, preserving co-reference coherence in the training data. Date shifts are format-preserving and consistent per document (180–730 day offset seeded by content hash), so temporal relationships between dates are preserved.

Entity types detected: `PERSON`, `PHONE_NUMBER`, `EMAIL_ADDRESS`, `LOCATION`,
`US_SSN`, `DATE_TIME`, `US_BANK_NUMBER`, `CREDIT_CARD`, `US_PASSPORT`,
`US_DRIVER_LICENSE`, `IP_ADDRESS`, `MEDICAL_LICENSE` (DEA), `US_NPI`,
`US_MBI`, `URL`, `STREET_ADDRESS`, `US_ZIP`, `AGE_OVER_89`,
`MEDICAL_RECORD_NUMBER`, `HEALTH_PLAN_ID`, `ACCOUNT_NUMBER`, `DEVICE_SERIAL`,
`VEHICLE_ID`, `OTHER_IDENTIFIER`, `UNRESOLVED_IDENTIFIER`.

Together these cover the sixteen HIPAA Safe Harbor identifier categories that
can appear in extracted text — the eighteen at 45 CFR §164.514(b)(2) less
full-face photographs and biometric identifiers, neither of which can.

**This is not a claim of Safe Harbor compliance.** Dates are shifted by a
consistent per-document offset to preserve clinical intervals, which is an
Expert Determination technique, not a Safe Harbor one — Safe Harbor requires
dates truncated to the year. Ages over 89 are aggregated to "90 or older".

### Documents held back

A document the scrubber cannot clean confidently is copied to
`output/review/pii_queue/` and no deliverable is written for it. Two triggers:

- **a recognised label with no readable value** — the document says "Medical
  Record Number:" and nothing follows it. Common on scans.
- **a healthcare identifier in a document classified as case law or
  published** — an SSN or medical record number does not belong in a published
  opinion, so finding one is evidence the document was classified wrongly.

Person, date and location never trigger it: those saturate legal text, and
treating them as a signal would hold back every real opinion.

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

## Review

There is no per-document approval step. The run completes on its own and
writes a record of every redaction; review is something you do to a finished
batch, not a gate the pipeline waits on.

`summary.html` is a static, read-only report — identifier types, counts and
locations, and never the identifiers themselves. It opens in any browser with
nothing running.

To check the values themselves, use the verification record:

```bash
python3.12 review_pii.py --list                       # batches holding values
python3.12 review_pii.py --batch <id> --summary       # counts by type/document
python3.12 review_pii.py --batch <id> --type US_SSN   # the values themselves
python3.12 review_pii.py --batch <id> --csv ~/check.csv
python3.12 review_pii.py --accept <id>                # delete the values
```

A CSV export from this tool carries original values, so it refuses to write
anywhere inside an output directory. A batch left unaccepted is named on the
next run rather than quietly aged out.

## After Running

1. Fill in `output/provenance.json` dataset-level fields: `dataset_name`, `created_by`, `source_collection`, `license`, `jurisdiction_coverage`
2. Check `output/review/pii_queue/` for documents held back, fix them, and re-run — re-runs replace rather than duplicate
3. Check `output/review/ocr_queue/` for PDFs needing manual OCR handling
4. Run the AI red-team verification: `ANTHROPIC_API_KEY=... python3.12 re_id_risk.py --output /path/to/output --apply`
5. Check the batch with `review_pii.py` and **accept it**, which deletes the original values

## File Structure

```
legal-doc-processor/
├── process.py          ← CLI entry point
├── pipeline.py         ← orchestrator
├── reader.py           ← PDF/DOCX/PPTX/EML/MSG/TXT → ReadResult
├── classifier.py       ← text + path → ClassifyResult (doc_type, confidence)
├── cleaner.py          ← strip headers, line numbers, Westlaw annotations
├── pii.py              ← Presidio detection, replacement, thresholds, seeding
├── recognizers.py      ← recognizer definitions for identifiers presidio omits
├── chunker.py          ← token-aware chunking via langchain-text-splitters
├── writer.py           ← RAG + finetune writers, and the redaction records
├── provenance.py       ← ProvenanceManifest with sidecar CSV support
├── reporter.py         ← generates summary.html + review_log.csv
├── review_pii.py       ← read the verification record; accept a batch
├── re_id_risk.py       ← AI red-team: adversarial re-identification assessment
├── second_pass.py      ← targeted second-pass redaction from red-team findings
├── SKILL.md            ← OpenClaw agent instructions
├── requirements.txt
└── tests/              ← pytest
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

## Legal Disclaimer

**This tool uses artificial intelligence to detect and redact personally identifiable information (PII). AI systems make mistakes.** No automated PII detection or re-identification risk assessment is foolproof. False negatives (missed PII) and false positives (incorrectly flagged text) are inevitable.

**The user is solely responsible for verifying that all output documents are free of PII before use.** This includes, but is not limited to:

- Reviewing the batch's verification record with `review_pii.py` before accepting it, and reading the `summary.html` report
- Running the AI red-team verification pass (`re_id_risk.py`) and reviewing its findings
- Checking `re_id_risk_report.json` for any `HIGH` or `CRITICAL` risk records and acting on the recommendations
- Inspecting the OCR queue for documents that bypassed automated processing
- Conducting your own independent review of a sample of output records before distributing or using the dataset

This tool is provided as a processing aid, not as a guarantee of compliance with any privacy law or regulation (including but not limited to HIPAA, GDPR, CCPA, state bar ethics rules, or court protective orders). Consult qualified legal counsel regarding your specific compliance obligations.

**By using this tool, you acknowledge that you understand these limitations and accept full responsibility for the accuracy and legality of any output.**

## License

MIT
