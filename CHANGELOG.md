# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/jimdawdy-hub/legal-doc-processor/releases/tag/v0.1.0
