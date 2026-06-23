# Academic Paper: PII Anonymization in Legal Documents

This directory contains an Arxiv-style LaTeX manuscript describing the `legal-doc-processor` approach to de-identifying personally identifiable information (PII) and quasi-identifiers from legal documents for ML dataset preparation.

## Files

| File | Description |
|------|-------------|
| `main.tex` | Full paper (abstract through references) |
| `README.md` | This file |

## Building a PDF

Requires a LaTeX distribution (TeX Live, MacTeX, or MiKTeX):

```bash
cd paper
pdflatex main.tex
pdflatex main.tex   # second pass for references
```

Output: `main.pdf`

## Arxiv submission notes

- Primary category suggestions: `cs.CL` (Computation and Language), `cs.CY` (Computers and Society), or `cs.LG` (Machine Learning)
- The paper is self-contained; no figures require external assets (pipeline diagram is inline)
- Fill in the repository URL in the `\author` block before submission
- Consider adding an explicit software availability statement with commit hash or release tag

## Markdown alternative

A Markdown rendering of the same content is available at `docs/pii-anonymization-paper.md` for readers who prefer not to compile LaTeX.
