# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.4.0] — 2026-06-14

### Added

- **Legal disclaimer** — Added prominent disclaimer to README stating that AI makes mistakes and the user is solely responsible for verifying all output is PII-free before use.
- **`utils.py`** — Shared module for `sha256_file()` and `SUPPORTED_EXTENSIONS`, eliminating duplication between `pipeline.py`, `provenance.py`, and `reader.py`.
- **Overlap-safe PII replacement** — `pii.py` now deduplicates overlapping Presidio entities before applying Faker substitutions, preventing offset corruption when Presidio returns overlapping spans.
- **Configurable Claude model** — `re_id_risk.py` accepts `--model` flag and `RE_ID_MODEL` env var (default: `claude-opus-4-7`), allowing cost-sensitive users to use cheaper models.
- **Concurrency locking** — `pipeline.py` uses `multiprocessing.Lock` to protect shared file writes (`review_log.jsonl`, `dataset.jsonl`) when `--workers > 1`.

### Changed

- **`pii.py`** — Replaced MD5 with SHA-256 for date offset seeding (consistency + avoids deprecation warnings on some Python builds).
- **`apply_decisions.py`** — Moved all imports to module level for fail-fast error detection.
- **`classifier.py`** — Added docstring documenting the `.txt` classification limitation.
- **`cleaner.py`** — Added comment explaining the numbered-list tradeoff in line-number stripping.
- **`chunker.py`** — Added docstring to `_get_encoder()` explaining the global singleton's concurrency semantics.
- **`pipeline.py`** — Added docstring to `_caselaw_meta()` documenting its regex limitations for non-standard citation formats.
- **`second_pass.py`** — Removed dead `pass` statement in file processing loop.

---

## [0.3.0] — 2026-05-17

### Added

- **`re_id_risk.py`** — Adversarial re-identification risk assessment. Samples the highest-PII-flag records from the finetune dataset and sends them to `claude-opus-4-7` with an adversarial privacy-expert framing. Identifies quasi-identifiers, reconstruction paths, and specific additional redactions. Writes `output/re_id_risk_report.json` and appends a risk section to `summary.html`. Run as `python3.12 re_id_risk.py --output /path/to/output [--samples 20]`. Requires `ANTHROPIC_API_KEY`.

### Changed

- **`requirements.txt`** — Added `anthropic>=0.68.0` for the Claude API SDK.
- **`SKILL.md`** — Documented `re_id_risk.py` in the post-run checklist and added interactive review commands.

---

## [0.2.0] — 2026-05-17

### Added

- **`reporter.py`** — Auto-generates two artifacts after every pipeline run: `summary.html` (interactive browser report with per-file results, PII flag tables, and decision toggles) and `review/review_log.csv` (all PII flags as a spreadsheet). No manual step required.

- **`review_server.py`** — Local HTTP server (`python3.12 review_server.py --output /path`) that serves `summary.html` and accepts decisions via POST. Enables fully in-browser review: click through flags, enter reviewer name, click "Apply Decisions" — server patches the dataset and writes the audit trail without any manual CSV handling.

- **`apply_decisions.py`** — Applies a `decisions.csv` to the finetune dataset. Re-runs PII stripping on affected files with restored entities excluded. Writes a `human_reviews` audit entry to `provenance.json` and archives the decisions CSV to the source case folder (`source_dir/audit/`).

- **`review_pii.py`** — Standalone CLI for reviewing PII flags: summary stats, per-file/per-type filtering, CSV export.

### Changed

- **`reader.py`** — Added `.txt` format support. Pre-extracted text files are now processed directly without re-OCR.

- **`pipeline.py`** — Added deduplication logic: exact SHA-256 duplicates are skipped (one processed); when `.txt`, `.ocr.pdf`, and original `.pdf` all exist for the same document, the best version is selected automatically (`.txt` > `.ocr.pdf` > `.pdf`). `source_dir` stored in provenance for audit trail routing.

- **`pii.py`** — DATE_TIME entities now use consistent per-document date shifting (180–730 day offset, seeded by document content hash) instead of random Faker dates. Temporal relationships between dates are preserved. Format-preserving: `March 15, 2024` → `May 26, 2025`, `3/15/2024` → `5/26/2025`. Chunked Presidio analysis handles documents > 900K characters (previously crashed on large medical records). Added explicit `PatternRecognizer` for US_SSN to ensure reliable detection.

- **`provenance.py`** — Added `source_path` per file and `source_dir` at dataset level. `human_reviews` array records each review pass with timestamp, reviewer name, decision counts, and path to archived decisions CSV.

---

## [0.1.0] — 2026-05-17

Initial release.

### Added

- **`classifier.py`** — Rule-based document classifier. Auto-detects `caselaw`, `published`, `private`, and `uncertain` types from content signals (citation patterns, email headers, attorney/party markers, journal metadata). Extension shortcuts for `.eml`, `.msg` (always private) and `.pptx` (always published).

- **`cleaner.py`** — Text normalizer. Strips Westlaw/Lexis annotation lines, leading line numbers (pleadings/transcripts), repeated header/footer lines, smart quotes, and excess whitespace.

- **`reader.py`** — Multi-format document reader. Supports PDF (pdfminer.six), DOCX (python-docx), PPTX (python-pptx), EML (stdlib email), MSG (extract-msg). OCR fallback via ocrmypdf + pytesseract with a quality gate: scanned PDFs scoring below 70% mean word confidence are routed to a review queue rather than included in the dataset.

- **`pii.py`** — PII detection and replacement. Uses Microsoft Presidio for entity detection with a supplemental `PatternRecognizer` for high-reliability SSN coverage. Entities scoring ≥ 0.85 are auto-redacted; entities scoring 0.50–0.84 are redacted and flagged to a review log. Finetune output uses Faker for natural synthetic replacements (consistent within a document); RAG output uses typed tokens (`[PERSON]`, `[EMAIL_ADDRESS]`, etc.).

- **`chunker.py`** — Token-aware text chunker backed by `langchain-text-splitters` and `tiktoken`. Case law: 512-token chunks with 64-token overlap, splitting on legal section headers first. Private/uncertain documents: 2048-token chunks with 128-token overlap.

- **`writer.py`** — JSONL output writers. RAG output: one file per source document, one record per chunk, with citation metadata for case law. Finetune output: one appended record per document with anonymized source ID (original filename kept only in provenance manifest).

- **`provenance.py`** — Dataset provenance manifest (`provenance.json`). Per-file records include SHA-256 hash of original source file, classification confidence, OCR flags, PII substitution counts, and review flag counts. Dataset-level fields (name, version, license, jurisdiction coverage) are written once and preserved across re-runs. Optional sidecar CSV (`--sidecar`) pre-populates `copyright_status` and `source_url_or_collection` per file.

- **`pipeline.py`** — Orchestrator. Runs all stages in order for each file; routes output by document type; supports parallel processing (`--workers`); aggregates results into provenance manifest.

- **`process.py`** — CLI entry point. Flags: `--input`, `--output`, `--dry-run`, `--workers`, `--sidecar`.

- **`SKILL.md`** — OpenClaw agent instruction file with usage, output layout, PII threshold table, and post-run checklist.

- **56 pytest tests** covering all modules.

[0.4.0]: https://github.com/jimdawdy-hub/legal-doc-processor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jimdawdy-hub/legal-doc-processor/releases/tag/v0.3.0
[0.2.0]: https://github.com/jimdawdy-hub/legal-doc-processor/releases/tag/v0.2.0
[0.1.0]: https://github.com/jimdawdy-hub/legal-doc-processor/releases/tag/v0.1.0
