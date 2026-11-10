# Code review — release 0.3.0

Reviewed November 10, 2026 against commit `2628670`, including every runtime Python module, tests, dependencies, CLI paths, generated HTML/CSV, and documentation. This is a standalone local legal-document processor, with optional external AI assessment. The assumed load is one trusted operator processing case folders, with worker processes inside a single batch. It is not a shared production web application.

The findings below describe the previous code. All listed fixes are included in this update. Tests use synthetic documents and mocked AI responses; no client document or paid model request was used.

## Must fix — resolved

1. **Classification bypassed privacy processing** — `pipeline.py`.
   - What: Classification selected the processing route.
   - Problem: A copyright footer or presentation extension could mark patient material as published and skip all PII checks.
   - Fix: Check every class by default; retaining public text requires an explicit option.
   - If unfixed: A routine document could leave with identifiers untouched.

2. **Overlaps and detection-window boundaries left identifiers behind** — `pii.py`.
   - What: Local detection returned character spans.
   - Problem: Discarding overlapping spans left detected tails; nonoverlapping windows missed boundary-spanning identifiers.
   - Fix: Merge selected spans by their union; overlap windows and deduplicate exact detections. Preserve medium-confidence review flags.
   - If unfixed: A green processing run could contain known identifier fragments.

3. **Medical/case labels and enabled NLP categories were incomplete** — `pii.py`.
   - What: The detector limited supported entity categories.
   - Problem: Common labeled record/member/case numbers, organization names, and URLs were omitted.
   - Fix: Add selected labeled identifiers and enable supported organization, URL, and nationality/religious/political-group categories.
   - If unfixed: Routine identifiers survive before any external AI request. Coverage remains incomplete; this is not healthcare de-identification certification.

4. **Distinct documents were silently discarded** — `pipeline.py`.
   - What: Deduplication grouped normalized filenames across folders and formats.
   - Problem: Two different `record.txt` files, or nonidentical formats sharing a stem, could be treated as one document.
   - Fix: Deduplicate identical bytes only.
   - If unfixed: The dataset omits evidence without a reliable equivalence test.

5. **Dataset identities were unstable and exposed filenames** — `pipeline.py`, `utils.py`.
   - What: Exports and later restores needed matching source identities.
   - Problem: Python's randomized filename hash changed between processes; RAG metadata exposed names, and duplicate basenames collided.
   - Fix: Stable opaque IDs derived from complete source paths; store their mapping only in the private manifest.
   - If unfixed: Restores can miss their records, or private filenames reach downstream systems. Opaque IDs are pseudonyms, not anonymization proof.

6. **Chunk limits did not limit exported analysis records** — `pipeline.py`, `writer.py`.
   - What: The pipeline calculated chunks but wrote an entire document as one record.
   - Problem: Large records exceeded advertised limits; only portions reached AI assessment.
   - Fix: Export the actual chunks, including public-source classes, with unique source/chunk identities.
   - If unfixed: Large portions remain unassessed despite apparent record-level coverage.

7. **Output reuse mixed datasets and lost audit history** — `pipeline.py`.
   - What: Reruns appended text but replaced provenance.
   - Problem: Old records remained without their original audit; output inside input could be ingested recursively.
   - Fix: Require a fresh, nonoverlapping batch directory and validate worker counts.
   - If unfixed: Reports no longer describe the dataset they accompany.

8. **A corrupt file prevented a usable batch result** — `pipeline.py`, `provenance.py`.
   - What: One file's extraction/hash failure propagated through the batch.
   - Problem: Previously written data could remain without a completed manifest.
   - Fix: Record failed files, finish other files and the manifest, then fail the batch explicitly.
   - If unfixed: Partial results can be mistaken for complete output.

9. **Sensitive local artifacts looked like shareable outputs** — `utils.py`, README.
   - What: Review logs, provenance, OCR originals, and AI findings intentionally retain identifiers.
   - Problem: The prior “PII-safe” presentation did not distinguish these from export candidates.
   - Fix: Private output permissions and prominent instructions never to upload the entire directory; clearly label confidential artifacts.
   - If unfixed: An anonymized text file can be undermined by the accompanying report or filename map.

10. **Unsafe review exports could execute document-supplied code** — `reporter.py`, `review_pii.py`.
    - What: Document text entered HTML scripts and spreadsheets.
    - Problem: A `</script>` string could break out of embedded JSON; spreadsheet formulas remained executable despite CSV quoting.
    - Fix: Escape script-sensitive characters and HTML fields; neutralize CSV formulas while validating round-trip decisions against canonical flags.
    - If unfixed: Opening a review artifact can execute untrusted content.

11. **The review API lacked adequate access and request checks** — `review_server.py`.
    - What: A local HTTP service applied restores and returned private information.
    - Problem: No session, permissive cross-origin headers, unbounded bodies, and weak validation allowed unauthorized reads or changes.
    - Fix: Loopback binding, private browser bootstrap, HttpOnly/SameSite session cookie, Host/Origin checks, anti-forgery token, bounded JSON, and no permissive CORS.
    - If unfixed: Another process or malicious webpage can reach sensitive review operations.

12. **Restores could undo privacy work or report success without a match** — `apply_decisions.py`.
    - What: Human review re-extracted originals and rewrote exports.
    - Problem: Unstable IDs, changed sources, duplicated CLI/server logic, and post-second-pass restores could reintroduce data or omit an export.
    - Fix: Validate current flags and source hashes; share the audit/apply path; stage re-analysis before replacing outputs; update both export formats; block restores after second-pass redaction.
    - If unfixed: A failed or apparently approved review can leave inconsistent or less-redacted data.

13. **External AI was vendor-bound and insufficiently gated** — `ai_client.py`, `re_id_risk.py`.
    - What: The red-team command transmitted processed text to one hard-coded provider/model.
    - Problem: It lacked an enforceable configuration gate for no-training terms or applicable ZDR, and used provider-specific caching/thinking options.
    - Fix: Explicit provider-neutral endpoint/model, reviewed Business/Enterprise policy, no-training declaration, conditional ZDR enforcement, explicit disclosure confirmation, and no redirects.
    - If unfixed: Sensitive text can reach an unapproved service. The new guard validates declarations; it cannot certify a vendor's conduct.

14. **AI failures could imply a LOW result** — `re_id_risk.py`.
    - What: Model responses and aggregate risk were accepted too loosely.
    - Problem: Valid JSON with the wrong shape, failed requests, or partial coverage could produce misleading summaries.
    - Fix: Validate the assessment schema; mark failed, sampled, or truncated coverage incomplete/UNKNOWN; block application on errors. Omit raw provider error bodies and malformed model responses.
    - If unfixed: Failed privacy checks look like successful clearance, or diagnostics repeat sensitive text.

15. **Retries and old reports targeted the wrong content** — `re_id_risk.py`, `second_pass.py`.
    - What: Retry lookup used one record per source; findings could be applied after changes.
    - Problem: Later chunks replaced earlier ones in lookup maps, and stale findings were not tied to their dataset.
    - Fix: Exact source/chunk identities, duplicate-ID rejection, and dataset fingerprints before retry/application.
    - If unfixed: A “retry complete” report can omit failed chunks or act on different text.

16. **In-place edits and regex replacements risked corruption** — `utils.py`, `second_pass.py`.
    - What: Dataset rewrites truncated their target before completion.
    - Problem: Interruptions could destroy the file; slashes split literal dates into broad fragments, and backslashes became replacement instructions.
    - Fix: Private temporary files and atomic replacement; preserve complete literal terms and literal replacement strings; refresh token metadata.
    - If unfixed: A redaction pass can corrupt content or remove unrelated numbers.

## Should fix — resolved

17. **Synthetic replacements were predictable or expensive** — `pii.py`.
    - What: Faker generated substitutes for detected values.
    - Problem: A global fixed seed repeated aliases, and every replacement generated every entity type before selecting one.
    - Fix: Independent random instance seeds and lazy generation of only the required substitute. Unparseable/overflowing dates become markers.
    - If unfixed: Predictable substitutes aid correlation; large files waste work. Date shifts still require privacy review.

18. **Cleaning removed substantive evidence** — `cleaner.py`.
    - What: Formatting cleanup removed numbered lines and repeated short text.
    - Problem: Addresses, numbered allegations, or repeated clinical findings could disappear.
    - Fix: Keep numbered/substantive repeated lines; restrict repeated-line removal to recognized boilerplate.
    - If unfixed: The cleaned document can lose meaning before anonymization begins.

19. **Unavailable OCR confidence did not hold a document back** — `reader.py`.
    - What: Scanned PDFs passed through an OCR quality gate.
    - Problem: Unknown confidence was accepted; string confidence values were ignored, and reader resources needed explicit cleanup.
    - Fix: Parse numeric confidence values, hold unavailable/low confidence, and close reader resources.
    - If unfixed: Unreliable extracted text can defeat both detection and review.

20. **Baseline tests were broken and risky paths lacked regressions** — tests, CI.
    - What: The existing suite had 56 tests.
    - Problem: Four fixtures referenced nonexistent source files; the review API, provider gate, retries, report injection, and restore path lacked protection.
    - Fix: Create real synthetic fixture files, add targeted regressions, and run the suite in GitHub Actions using pinned actions.
    - If unfixed: Future changes can silently restore the old failures.

## Verification

- 108 tests passed locally on Python 3.12; the original PII tests remain intact. Four pre-existing provenance fixture failures were corrected.
- Focused Ruff undefined-name/unused-import checks and Python compilation passed.
- Generated review JavaScript passed Node's syntax check with a malicious document-text fixture.
- A real two-worker CLI smoke run retained two distinct same-named documents, removed the synthetic identifiers, and produced private output permissions.
- No real client documents or paid AI endpoints were used. The only network transport test sends synthetic data to local HTTP servers and verifies that redirects are refused.

## Remaining limits / not checked

- Automated redaction is incomplete. Context, rare combinations, case-specific codes, non-English text, OCR errors, and cross-document linkage need human and legal assessment. No real-client corpus quality benchmark was performed.
- Only extracted text is handled. The original document, images, attachments, document metadata, and any unprocessed OCR copy remain confidential.
- Local provenance, review records, and risk findings retain original or residual identifiers. They must stay local; retention and deletion are operator responsibilities. File modes do not encrypt data.
- The AI policy file is an operator-reviewed declaration, not remote verification of contracts or vendor settings. The tool does not decide a jurisdiction's legal obligations; consult a privacy attorney.
- File replacements are atomic individually. A power/disk failure during a multi-file commit is not a filesystem-wide transaction. Concurrent independent reviewers/processors against one batch are unsupported; preserve originals and inspect any failed operation before use.
- HTTP compatibility and provider retention behavior were not tested against live paid services. Python 3.12 is tested; other Python versions and a fresh operating-system installation were not locally validated.

**Verdict:** the identified defects are fixed and the tool is provider-neutral, with explicit no-training controls. It remains an aid for reviewed legal-document processing, not a privacy clearance system.

**Lean:** removed the vendor SDK and unused anonymizer dependency, made date parsing an explicit dependency, consolidated duplicated review application logic, and removed obsolete assistant packaging.
