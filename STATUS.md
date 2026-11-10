# Release 0.3.0

- Full source review and privacy hardening complete; findings and limits: `docs/code-review.md`.
- Standalone tool; assistant-specific packaging removed.
- Vendor-neutral red team requires an explicit approved endpoint/model and a reviewed no-training Business/Enterprise policy. Required-but-disabled ZDR blocks requests.
- README prominently states the commercial plan, disabled-training, circumstances/jurisdiction-dependent ZDR, and privacy-attorney requirements.
- Validation: 108 tests passed; Ruff F checks, generated JavaScript syntax, and real two-worker synthetic CLI smoke passed. No client data or paid model calls used.
- GitHub publication and release follow validation. Preserve the historical v0.4.0 tag; publish the previously absent v0.3.0 tag at the new commit.
