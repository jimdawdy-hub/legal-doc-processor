# Legal Document Processor

A standalone Python tool for extracting text from legal documents, detecting personal information, and producing redacted JSONL datasets (one JSON record per line). It runs from the command line and does not require an AI assistant, plugin, or agent skill.

The document-processing pass runs locally. An **optional, vendor-agnostic AI red-team pass** can assess residual re-identification risk through an explicitly approved HTTP endpoint. No provider or model is selected automatically.

> **Privacy requirement for external AI use**
>
> **Even anonymized or redacted PII sent to an AI for red-team analysis must be processed under a Business or Enterprise-level plan, with training on user data turned off.** A commercial API agreement must provide those same Business/Enterprise-level protections. Do not approve a vendor/service/model that trains on submitted user data. A paid consumer subscription or a model name alone does not satisfy this project's policy.
>
> **Zero Data Retention (ZDR) may also be required depending on the circumstances and jurisdiction.** Verify the applicable professional duties, client authorization, protective orders, confidentiality obligations, data-processing terms, subprocessors, residency, and retention settings before transmitting any material. A Business/Enterprise subscription and disabled training do **not** automatically provide ZDR. Confirm that ZDR actually covers the selected endpoint, model, and features when it is required.
>
> **Consult a privacy attorney if in doubt.** These are project usage requirements, not a claim that one subscription tier establishes legal compliance everywhere. Redaction does not by itself establish anonymity, preserve privilege against every disclosure, or authorize an upload.

## What it processes

- PDF, DOCX, PPTX, EML, MSG, and UTF-8 text.
- Local text extraction and OCR for scanned PDFs. Low or unavailable OCR confidence holds the file for local review.
- PII detection using Microsoft Presidio, a local spaCy model, and supplemental labeled medical/case identifiers.
- Typed redaction markers by default; optional synthetic replacements for authorized testing or dataset preparation.
- A local provenance manifest, review report, and human-review audit.
- Optional AI assessment, followed by a separately requested second redaction pass.

The JSONL workflow processes extracted text. The medical PDF workflow below creates a new PDF. Treat source files and intermediates as confidential. The processor does not certify HIPAA Safe Harbor or GDPR anonymization.

## Install

Python **3.12 is tested**. Use a dedicated environment; dependency/model compatibility may differ on newer Python releases.

```bash
git clone https://github.com/jimdawdy-hub/legal-doc-processor.git
cd legal-doc-processor
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m spacy download en_core_web_lg
```

Install Tesseract and OCRmyPDF system dependencies for scanned PDFs. Install the spaCy model before processing: the tool refuses to download an NLP model implicitly during a run. Tokenizer assets may require an initial download/cache population. No paid AI calls are needed for local processing or tests.

## Local processing

Use a new, empty output directory outside the input directory for each batch:

```bash
# Classify and estimate chunks without writing output or running PII detection
python process.py --input /path/to/documents --output /path/to/new-output --dry-run

# Local redaction, with typed markers such as [PERSON] and [US_SSN]
python process.py --input /path/to/documents --output /path/to/new-output

# Optional parallel processing
python process.py --input /path/to/documents --output /path/to/new-output --workers 2

# Optional synthetic values; these are invented facts, not factual corrections
python process.py --input /path/to/documents --output /path/to/new-output --replacement synthetic
```

Classification (`caselaw`, `published`, `private`, `uncertain`) controls chunk size and RAG routing (retrieval-augmented generation). **It does not establish that a document is public or exempt it from redaction.** All classes pass through PII detection by default, including presentations and documents carrying copyright footers.

`--preserve-public-text` explicitly bypasses redaction for documents classified as `caselaw` or `published`. Use it only in a separately verified public-source batch; classification can be wrong. Keep it off for client files, medical records, mixed collections, and other confidential material.

Only byte-identical files are deduplicated. Different documents with the same filename or different extensions are retained. Dataset IDs are stable opaque hashes of source paths, not Python's process-dependent `hash()`. These IDs are pseudonyms, not proof of anonymity.

An unreadable/corrupt file is recorded in the local manifest while other files finish. The batch exits unsuccessfully if any processing errors occur; inspect its manifest before using any partial output. A nonempty output directory is refused to prevent appending duplicate records while overwriting the prior audit.

## Outputs and confidentiality

### Extract positioned PDF text on a local GPU

Use `pdf_ocr.py` to get text, line confidence, and word boxes for each PDF page. The command keeps selectable text on digital pages and runs OCR on scans, including scans with an existing hidden text layer.

Prepare a Python environment with PyMuPDF, PaddleOCR 3.7, and ONNX Runtime GPU with matching CUDA libraries. Cache the PP-OCRv6 Medium models before opening private inputs. Run OCR with networking disabled. [Detector weights](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det_onnx) · [Recognizer weights](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec_onnx)

```sh
umask 077
mkdir -m 700 private-case
CUDA_VISIBLE_DEVICES=0 python pdf_ocr.py /path/to/source.pdf \
  --detector /path/to/detector --recognizer /path/to/recognizer \
  --output private-case/positioned-text.jsonl
```

The transcript contains original text. To use two GPUs, run one process per GPU with `--shards 2`, distinct `--shard` values `0` and `1`, and distinct output files. Merge the two transcripts in page order with `jq -cs 'sort_by(.number)[]' private-case/shard-0.jsonl private-case/shard-1.jsonl > private-case/positioned-text.jsonl` before building the PDF.

### Build a synthetic medical PDF

Run the PDF workflow in an owner-only directory with networking disabled. It uses positioned OCR, the installed spaCy model, and format-specific rules. It replaces each recognized identity consistently across the package and shifts complete calendar dates by an explicitly chosen number of days. Partial or malformed dates count toward review. It rebuilds the pages from edited pixels and adds a searchable layer from the edited transcript, so source attachments, form objects, and hidden text stay out of the new PDF.

Install Tesseract for the independent OCR check and `zxing-cpp` if using barcode masking. Use a local TrueType font with English and medical punctuation coverage. The code blocks use example paths and a sample date shift.

```sh
python pdf_review.py scan /path/to/source.pdf --output private-case/independent-source.jsonl
python pdf_synthetic.py /path/to/source.pdf \
  --transcript private-case/positioned-text.jsonl \
  --independent-source private-case/independent-source.jsonl \
  --date-shift-days 45 --ner-model en_core_web_lg --font /path/to/local-font.ttf \
  --aliases private-case/entity-aliases.json \
  --key-file private-case/case.key --barcodes --output private-case/synthetic.pdf
python pdf_review.py scan private-case/synthetic.pdf \
  --output private-case/independent-output.jsonl
python pdf_review.py compare --transcript private-case/positioned-text.jsonl \
  --independent-source private-case/independent-source.jsonl \
  --independent-output private-case/independent-output.jsonl \
  --date-shift-days 45 --ner-model en_core_web_lg --key-file private-case/case.key \
  --aliases private-case/entity-aliases.json \
  --private-flags private-case/visual-review.json
```

Record the confirmed patient name in the private `--aliases` file before building and reviewing the PDF. Each entry has a `role` and a `values` array of equivalent spellings, with the preferred full name first. For a compound patient surname, supply a `surname` field. A standalone surname uses the same synthetic surname as the full name. The independent visual check looks for the confirmed full names and surname, including occurrences the replacement pass missed. It fails if a confirmed name survives or if neither source OCR pass matched the confirmed patient aliases.

To use one synthetic full name throughout a package, declare the confirmed components in `identity` and the desired full name in `replacement`. The parser matches full and partial names, initials, and suffixes in any order. It handles names split across nearby fields and letters split by OCR. Independent review flags original components and incomplete or mixed synthetic names.

```json
[
  {
    "role": "patient",
    "identity": {
      "given": "Avery",
      "middle": ["Quinn"],
      "surname": "Vale",
      "suffix": "Jr."
    },
    "values": ["Vale Jr., Avery Q."],
    "replacement": "Morgan Finch"
  }
]
```

An optional `--candidates` JSONL file adds findings from a separate offline model. Each row has a one-based `page` and `candidates` arrays of zero-based line index, start, end, and role. Both paths use the same case-wide replacement map.

The comparison reports counts and can save page and text offsets for private review. Check flagged pages, billing figures, handwriting, signatures, and identifiers present only in graphics before sharing a copy. Keep the OCR transcripts, case key, aliases, and review files with the source records.

Replay confirmed position fixes with `--local-repairs` on both the build and comparison. Each entry records the one-based page, zero-based source line, character start and end, role, measured page box, and optional corrected reading. Use line index `-1` with a reading to add a field the first OCR engine missed. Treat this file as confidential because it can contain original readings.

### Correct a name in a reviewed PDF

Use `pdf_name_repair.py` on a synthetic scan PDF produced by this workflow. Put the confirmed fixes in a private JSON file. For example, `[{"from": "Avery", "to": "Morgan"}]` changes the spelling wherever it occurs, including inside a longer word. The command matches uppercase, lowercase, and mixed-case readings and handles OCR gaps between letters.

```sh
python pdf_name_repair.py private-case/synthetic.pdf \
  --replacements private-case/name-fixes.json --font /path/to/local-font.ttf \
  --output private-case/corrected.pdf
```

The repair edits words on the page images and in the searchable text. It checks dates and amounts and reruns local OCR in three modes before publishing the new file. The receipt contains counts. To check for a word the OCR modes missed, supply `--reference` with the original PDF and one or more `--reference-transcript` files. The repair compares those source positions against the current page image before changing it.

### Redacted dataset files

```text
new-output/
├── rag/                         # redacted retrieval chunks for public-source classes
├── finetune/dataset.jsonl        # redacted analysis chunks for ALL classes
├── review/
│   ├── review_log.jsonl          # CONFIDENTIAL: original detected values and context
│   ├── review_log.csv            # CONFIDENTIAL: spreadsheet review export
│   ├── decisions-*.csv           # CONFIDENTIAL: human-review decisions
│   └── ocr_queue/               # CONFIDENTIAL: copies of unprocessed originals
├── provenance.json              # CONFIDENTIAL: original filenames, paths, hashes, audit
├── summary.html                 # CONFIDENTIAL: interactive review of original values
└── re_id_risk_report.json        # CONFIDENTIAL: may reproduce residual identifiers
```

**Never upload or distribute the whole output directory.** The report, provenance, review files, OCR queue, and AI findings deliberately retain information for local verification. Keep them on protected storage with appropriate access controls, encryption, backups, and deletion procedures. New batch directories are owner-only; generated files use private permissions. File permissions alone do not provide encryption or a retention policy.

Only individually reviewed files in `rag/` or `finetune/` are candidates for a separately authorized transfer, and the external-AI requirements above still apply. The historical `finetune` directory name is retained for compatibility: it contains text chunks for review/analysis, not permission to train a model on client information.

Each JSONL record has `text` and `metadata`, including an opaque source ID, document type, chunk position, and token count. `pii_stripped: true` means detection/replacement ran; it does **not** mean all identifying information was removed. Private records are split into approximately 2,048-token chunks; public-source classes use approximately 512-token chunks. Model tokenization and boundary behavior still need validation for the intended downstream system.

## Optional AI red-team assessment

**This step transmits already-processed text to the selected endpoint.** Remaining quasi-identifiers can identify someone through combinations of facts, such as a facility, rare procedure, incident date, or case number. The second redaction pass cannot undo the disclosure that enabled the AI to discover those identifiers. Do not send raw originals, local audit files, or identifiers as provider error diagnostics.

The adapter uses a Chat Completions-style JSON HTTP interface (`model`, `messages`, `max_tokens`; response `choices[].message.content`). Choose a compliant vendor or approved gateway implementing that interface. It uses no vendor SDK (client library), embedded API key, default endpoint, or hard-coded model. Redirects are refused so credentials and document text are not silently sent to another destination.

### No-training policy guard

Copy `ai-policy.example.json` to a protected local file and fill it in **only after reviewing the provider's agreement and your organization/project settings**. The example is intentionally incomplete and cannot authorize an upload.

```json
{
  "endpoint": "https://approved-provider.example/v1/chat/completions",
  "models": ["approved-model-id"],
  "plan": "business",
  "training_on_user_data": false,
  "zdr_required": true,
  "zdr_enabled": true,
  "policy_reference": "Reviewed contract and tenant settings; reference held locally"
}
```

- `plan` must be `business` or `enterprise`, describing the actual commercial agreement.
- The endpoint and model must exactly match the reviewed policy.
- Training must be explicitly `false`; true, missing, or unknown values are rejected.
- Both ZDR fields must be explicit booleans. If ZDR is required but not enabled, the tool refuses the request. Set `zdr_required` only after determining what your circumstances and jurisdiction require.
- Record the reviewed contract/settings in `policy_reference`.

**The guard checks your supplied policy, not the truth of a vendor's promises.** It cannot inspect every provider contract, certify tenant settings, detect an undisclosed policy change, or guarantee a gateway's behavior. Recheck the policy when terms, models, features, or circumstances change. Do not approve training-enabled services.

Set `RE_ID_API_KEY` securely in the environment when the endpoint requires a bearer key. Do not place keys in URLs, source control, or command arguments. Then run:

```bash
python re_id_risk.py --output /path/to/new-output \
  --api-url https://approved-provider.example/v1/chat/completions \
  --model approved-model-id \
  --ai-policy /private/path/ai-policy.json \
  --confirm-approved-ai-use
```

`RE_ID_API_URL`, `RE_ID_MODEL`, and `RE_ID_POLICY` are equivalent configuration variables. `--confirm-approved-ai-use` is required in addition to the policy file; it acknowledges the disclosure and the Business/Enterprise, no-training, and applicable retention requirements.

All analysis records are assessed by default. `--samples N` explicitly limits the sample and cannot clear unassessed records. Legacy oversized records are sampled at the beginning, middle, and end (up to roughly 24,000 characters); truncated assessments are marked incomplete. The model is asked for residual identifiers, reconstruction risks, recommended redactions, and a risk level. Its response is schema-checked, not accepted as proof of anonymity.

Failed/malformed results and partial assessments produce `UNKNOWN` overall risk rather than a misleading `LOW`. Retry failures using the same configuration plus `--retry-errors`. Retry identities include the chunk index, so chunks of the same document are not confused. A changed dataset requires a fresh assessment.

```bash
# Preview targeted additional redactions locally; no AI call
python second_pass.py --output /path/to/new-output --dry-run

# Apply reviewed findings, optionally adding a local term list
python second_pass.py --output /path/to/new-output --blocklist /private/path/terms.txt
```

`--apply` on the assessment command requests automatic second-pass redaction after successful responses. Assessment errors block application. A partial sample is still a partial assessment, even if its known findings are applied. Applying changes makes the prior assessment stale; reassess the new text before relying on its risk result. Regex matches use literal strings, not model-supplied executable patterns.

## Human review

```bash
python review_server.py --output /path/to/new-output
```

The server binds only to loopback and opens a private browser session. Its bootstrap link is saved in an owner-only file under `review/` rather than printed to logs. If no browser is available, use `--no-open` and open that file locally to obtain the link. The UI checks the session, request origin, and a review token before applying decisions. Do not expose it on a public interface.

Or download decisions from `summary.html` and apply them locally:

```bash
python apply_decisions.py --decisions /private/path/decisions.csv \
  --output /path/to/new-output --reviewer "Reviewer name"
```

Decisions must match current flag IDs and detected values. A restore reprocesses the original source and updates both dataset exports. Changed/missing originals and legacy unstable IDs are refused. Restoring after second-pass redaction is blocked because it could reintroduce removed identifiers; regenerate and reassess instead. A restore intentionally puts original text back into the output, so review the resulting data again.

## Provenance and source permissions

Use `--sidecar sources.csv` to add local copyright/source information:

```csv
filename,copyright_status,source_url_or_collection,notes
public_opinion.pdf,public domain,https://courtlistener.com/opinion/123/,Verify source and jurisdiction
```

Processing or anonymizing a document does not grant copyright, licensing, confidentiality, or dataset resale rights. Provenance and sidecar records stay local.

## Tests and review

```bash
python -m pytest tests/ -q
```

Tests use synthetic fixtures and mocked AI responses; they do not send client documents to a provider. See [the code review](docs/code-review.md) for fixes, validation, and remaining limits. Older design documents are historical, not current installation or privacy instructions.

No detector catches every identity, language, OCR error, or combination of facts. This tool is a processing aid, not legal advice or a compliance guarantee. You remain responsible for verifying outputs and any disclosure. Consult a privacy attorney when uncertain.

Policy sources to verify for your chosen provider and matter: [ABA Formal Opinion 512](https://www.americanbar.org/content/dam/aba/administrative/professional_responsibility/ethics-opinions/aba-formal-opinion-512.pdf), [an example of commercial no-training terms](https://openai.com/enterprise-privacy/), and [an example of endpoint-specific retention/ZDR controls](https://developers.openai.com/api/docs/guides/your-data). Provider examples do not constitute approval of a vendor, plan, or configuration.

## License

MIT. Source-document rights are separate from this code's license.
