# Hybrid De-identification of Legal Documents for Machine Learning: A Multi-Stage Pipeline Combining NER, Pseudonymization, and LLM-Guided Quasi-Identifier Redaction

**Based on the `legal-doc-processor` open-source system**

---

## Abstract

Preparing legal corpora for retrieval-augmented generation (RAG) and fine-tuning requires de-identification that goes beyond standard named-entity recognition (NER). Legal pleadings, client communications, and medical-malpractice materials retain *quasi-identifiers*—case numbers, uncommon surnames, treating physicians, and facility-specific narratives—that can re-identify individuals even when conventional personally identifiable information (PII) has been removed.

We describe `legal-doc-processor`, an open-source pipeline that (1) classifies documents by privacy sensitivity, (2) applies confidence-gated NER-based detection with pseudonymization for training data, (3) routes borderline detections to human review, and (4) optionally subjects outputs to an adversarial large language model (LLM) re-identification assessment followed by targeted second-pass redaction.

On a real personal-injury malpractice folder (60 source documents, 49 fine-tuning records), a first pass using Microsoft Presidio produced 7,145 entity flags, yet 18 of 20 sampled records remained at HIGH or CRITICAL re-identification risk; a second pass applied over 15,000 additional substitutions. We argue that legal-document de-identification for ML should be treated as a *hybrid* problem spanning NER, domain routing, pseudonymization policy, human oversight, and quasi-identifier analysis rather than as a single NER pass.

---

## 1. Introduction

Law firms, courts, and legal technology vendors increasingly seek to convert document collections into datasets suitable for large language model (LLM) fine-tuning and retrieval-augmented generation (RAG). Unlike published case law, client-facing materials—pleadings, emails, deposition transcripts, and internal case notes—contain sensitive identifiers subject to privacy regulation, protective orders, and professional ethics obligations.

Automated de-identification tools developed for clinical text (e.g., HIPAA Safe Harbor field removal) or general-purpose PII scanners often underperform on legal corpora for three reasons:

1. **Domain-specific identifiers.** Court docket numbers, caption lines, and attorney-of-record blocks are not standard PII types in off-the-shelf recognizers.
2. **Quasi-identification.** A combination of procedure type, incident date, facility, and geography may uniquely identify a plaintiff without any surviving explicit name.
3. **Dual use of public material.** Published opinions and CLE materials should remain citation-rich for RAG, while private matter files require aggressive stripping—a single corpus may require *selective* de-identification.

This paper documents the approach implemented in `legal-doc-processor`, an open-source Python pipeline that ingests PDF, DOCX, PPTX, EML, MSG, and plain-text files and emits JSONL datasets with provenance manifests. Our contribution is architectural and empirical rather than algorithmic: we describe a production-oriented multi-stage design, specify its confidence-gated replacement policies, and report operational results from a malpractice case folder illustrating systematic failure modes of NER-only de-identification.

---

## 2. Related Work

**Clinical de-identification.** Systems such as MIT's de-id tools, cTAKES, and commercial HIPAA-oriented products target clinical notes with annotated corpora (e.g., i2b2). Legal text differs in structure (captions, procedural posture, citations) and in the prominence of quasi-identifiers outside HIPAA's 18 Safe Harbor categories.

**General PII detection.** Microsoft Presidio combines spaCy NER with regex recognizers and context-aware scoring. It is effective on emails, phone numbers, and Social Security numbers but does not treat court case numbers or law-firm matter identifiers as first-class entities.

**Privacy models.** Formal *k*-anonymity, *l*-diversity, and differential privacy provide mathematical guarantees under stated adversary models. The system described here does *not* implement or verify such guarantees; it instead pursues *operational* risk reduction through detection, pseudonymization, audit trails, human review, and adversarial LLM assessment.

**LLM-assisted privacy.** Recent work explores LLMs for PII detection and document redaction. We use an LLM not as the primary detector but as an *adversarial reviewer* after a conventional first pass, targeting quasi-identifiers that survive NER.

---

## 3. System Overview

The pipeline is modular: each stage exposes a narrow interface orchestrated by a single entry point (`process.py`).

```
Input (PDF, DOCX, PPTX, EML, MSG, TXT)
  → Read (text extraction; OCR if needed)
  → Classify (caselaw / published / private / uncertain)
  → Clean (legal noise removal)
  → De-identify (private & uncertain only)
  → Chunk (token-aware splitting)
  → Write (RAG and/or finetune JSONL)
  → Provenance & reports
  → [Optional] LLM re-ID risk assessment
  → [Optional] second-pass redaction
  → [Optional] human review corrections
```

### 3.1 Document-type routing

Classification is rule-based (no ML classifier), scoring regex signals over the first 5,000 characters plus extension shortcuts.

| Type | PII stripped | RAG output | Finetune output |
|------|--------------|------------|-----------------|
| `caselaw` | No | Yes (512 tokens) | No |
| `published` | No | Yes (512 tokens) | Yes (full doc) |
| `private` | Yes | No | Yes (2048 tokens) |
| `uncertain` | Yes (conservative) | No | Yes (flagged) |

Extension shortcuts assign high-confidence labels: `.eml` and `.msg` → `private` (0.99); `.pptx` → `published` (0.90). Content signals include federal and state citation patterns (`caselaw`), volume/issue/ISSN markers (`published`), and email headers, party captions, and attorney blocks (`private`). If the winning normalized score falls below 0.60, the document is labeled `uncertain` and treated as `private` for PII purposes—a conservative default for ambiguous client material.

### 3.2 Ingestion and OCR quality gate

Text PDFs are extracted with `pdfminer.six`; scanned PDFs trigger `ocrmypdf` and Tesseract when extracted text falls below 100 characters. Mean word confidence is computed from hOCR output on the first three pages. Documents with mean confidence below 70% are copied to a review queue and excluded from the dataset, reducing OCR-induced entity-boundary errors that propagate into missed PII.

### 3.3 Legal text cleaning

Before detection, a cleaning stage removes artifacts common in legal corpora: Westlaw/Lexis annotation lines, repeated short headers and footers (≥3 occurrences), leading line numbers in pleadings, smart-quote normalization, and excess whitespace.

---

## 4. First-Pass De-identification

### 4.1 Detection stack

Detection uses Microsoft Presidio's `AnalyzerEngine` with spaCy's `en_core_web_lg` model and twelve English-language entity types: `PERSON`, `PHONE_NUMBER`, `EMAIL_ADDRESS`, `LOCATION`, `US_SSN`, `DATE_TIME`, `US_BANK_NUMBER`, `CREDIT_CARD`, `US_PASSPORT`, `US_DRIVER_LICENSE`, `IP_ADDRESS`, and `MEDICAL_LICENSE`.

A supplemental `PatternRecognizer` registers the regex `\b\d{3}-\d{2}-\d{4}\b` for US Social Security numbers at confidence 0.90, with context words *ssn* and *social security*, ensuring formatted SSNs are caught even when Presidio's validation scoring is weak.

Documents exceeding 900,000 characters are analyzed in chunks to remain under spaCy's approximate one-million-character limit; entity offsets are adjusted to global positions before replacement.

### 4.2 Confidence-gated triage

Each detected span receives a Presidio confidence score *s* ∈ [0, 1].

| Score | Action |
|-------|--------|
| *s* ≥ 0.85 | Auto-redact |
| 0.50 ≤ *s* < 0.85 | Redact and log to human review queue |
| *s* < 0.50 | Retain (accepted false-negative risk) |

Medium-confidence entities are redacted *and* written to `review_log.jsonl` with ±60 characters of context, entity type, original text, and confidence—enough for a reviewer to distinguish, e.g., a judge's name from a client's name without exposing the full document in the log.

Overlapping spans are deduplicated before replacement by keeping the earliest-starting span, preventing offset corruption from nested entities.

### 4.3 Replacement strategies

**Fine-tuning (pseudonymization).** Detected strings are replaced with type-matched synthetic values from Faker. A per-document substitution table ensures co-reference consistency: identical original strings map to identical fake values within a document, preserving narrative coherence for QLoRA-style fine-tuning.

**Dates.** `DATE_TIME` entities are not replaced with random Faker dates. Instead, each document receives a stable offset Δ in days:

> Δ = 180 + (*h* mod 551)

where *h* is the integer value of SHA-256 applied to the first 4,096 characters of document text. All dates in the document are shifted by Δ using format-preserving reformatting. This preserves temporal intervals between events—often legally salient—while removing calendar dates that could support re-identification.

**RAG (token masking).** For retrieval-oriented output, entities are replaced with typed bracket tokens (`[PERSON]`, `[EMAIL_ADDRESS]`, etc.). The main pipeline does not emit RAG chunks for `private` documents.

**Metadata anonymization.** Fine-tuning records use anonymized source identifiers of the form `anon_XXXXXXXX` derived from a hash of the original filename. Original filenames and SHA-256 hashes of source files are retained only in `provenance.json`.

---

## 5. Human-in-the-Loop Review

After each run, an interactive HTML summary and CSV export present all medium-confidence flags. Reviewers may batch-approve redactions or mark false positives for *restoration*. Approved restorations trigger a re-processing pass that excludes specified entity strings from redaction and patches the dataset. Decisions are archived with reviewer identity and appended to the provenance manifest.

This design reflects a practical trade-off: fully automated redaction maximizes recall at the cost of false positives, while manual review of *only* borderline detections keeps human effort bounded.

---

## 6. Adversarial Re-identification Assessment

### 6.1 Motivation

NER-based first passes excel at structured PII but systematically miss legal quasi-identifiers.

| Missed item | Cause | Second-pass action |
|-------------|-------|-------------------|
| Uncommon surnames | Absent from spaCy NER training | `[PERSON]` |
| Court case numbers | Not a standard PII type | `[CASE_NO]` |
| Treating physicians | OCR errors; header layout | `[PERSON]` |
| Named facilities | Organizations not HIPAA PHI | `[ORGANIZATION]` |
| Quasi-ID combinations | No single field exceeds NER threshold | Narrative analysis → targeted terms |

### 6.2 LLM adversary model

The optional module `re_id_risk.py` sends fine-tuning records to a frontier LLM (default: Claude Opus) under an adversarial system prompt. The modeled adversary has access to federal and state dockets (PACER, CourtListener), news archives, provider directories, property and licensing records, and web search.

For each record, the model returns structured JSON containing risk level, quasi-identifiers, a reconstruction path, additional redaction phrases, and a confidence score.

Long documents are sampled at three 8,000-character segments (beginning, middle, end). Records are prioritized by descending Presidio review-flag count.

### 6.3 Second-pass redaction

`second_pass.py` aggregates quasi-identifier values across all assessments, maps types to domain-specific tokens, compiles case-insensitive regex patterns with word-boundary anchoring, and applies substitutions in-place to all finetune and RAG JSONL files. Unlike the first pass, the second pass uses irreversible token masking rather than Faker pseudonymization.

| Quasi-identifier types | Token |
|------------------------|-------|
| `patient_surname`, `treating_physician`, `attorney_names_and_firms` | `[PERSON]` |
| `facility`, `hospital_facility`, `defendant_entity` | `[ORGANIZATION]` |
| `case_number`, `docket_number`, `court/venue` | `[CASE_NO]` |
| `incident_date`, `date_of_birth`, `filing_date` | `[DATE]` |
| `mrn`, `medical_record_number`, `ardc_number` | `[ID_NUMBER]` |

---

## 7. Empirical Case Study

Results from processing a real personal-injury medical-malpractice document folder:

| Metric | Value |
|--------|-------|
| Source documents | 60 |
| Fine-tuning records emitted | 49 |
| Presidio first-pass entity flags | 7,145 |
| Red-team sample size | 20 records |
| Sampled records at HIGH or CRITICAL risk post-Presidio | 18 / 20 (90%) |
| Second-pass additional substitutions | 15,000+ |

The gap between 7,145 first-pass flags and 90% HIGH/CRITICAL residual risk indicates that *entity count alone is a poor proxy for de-identification quality* on legal matter files.

**Limitations.** The case study covers a single matter type (US personal-injury malpractice), lacks a labeled gold-standard benchmark, and the red-team sample (*n* = 20) was prioritized by review-flag count. No precision/recall/F1 figures against expert annotations are reported.

---

## 8. Discussion

**Pseudonymization vs. redaction for ML.** Faker-based pseudonymization preserves readable training text and co-reference structure. It is not equivalent to irreversible redaction. The two-pass design—pseudonymize first for naturalism, then token-mask flagged quasi-identifiers—reflects a pragmatic sequencing for legal ML datasets.

**Selective de-identification.** Routing `caselaw` and `published` documents around PII stripping preserves citations needed for RAG over public law. Misclassification is a primary systemic risk; conservative handling of `uncertain` documents mitigates false negatives.

**What this system does not guarantee.** The implementation explicitly disclaims regulatory compliance (HIPAA, GDPR, CCPA, bar ethics, protective orders). Low-confidence detections (*s* < 0.50) are retained by policy. No *k*-anonymity, *l*-diversity, or differential privacy bounds are computed.

**Operational recommendations:**

1. Treat document classification as a privacy gate, not merely a metadata label.
2. Log and review medium-confidence detections.
3. Run adversarial quasi-identifier assessment before releasing fine-tuning corpora from client matter files.
4. Maintain provenance manifests linking anonymized outputs to hashed originals.
5. Exclude low-confidence OCR outputs rather than feeding noisy text into NER.

---

## 9. Conclusion

Legal-document de-identification for machine learning requires a hybrid architecture that combines off-the-shelf NER, domain-aware routing, confidence-gated pseudonymization, human review of borderline cases, and LLM-guided adversarial assessment for quasi-identifiers. Empirical results on a malpractice matter folder demonstrate that conventional Presidio-based first passes, despite thousands of entity flags, leave the majority of assessed records at high re-identification risk; a second targeted pass closes much of that gap. Future work should include annotated legal de-identification benchmarks, systematic evaluation of classification error rates, and formal privacy metrics under explicit adversary models.

---

## References

1. Microsoft Presidio. https://github.com/microsoft/presidio
2. Honnibal et al. spaCy: Industrial-strength Natural Language Processing in Python.
3. Sweeney, L. *k*-anonymity: A model for protecting privacy. *International Journal of Uncertainty, Fuzziness and Knowledge-Based Systems*, 10(5), 2002.
4. U.S. Dept. of Health and Human Services. HIPAA Safe Harbor de-identification standard, 45 C.F.R. § 164.514(b).
5. Machanavajjhala et al. *l*-diversity: Privacy beyond *k*-anonymity. *ACM TKDD*, 2007.

---

*LaTeX source for Arxiv submission: `paper/main.tex`*
