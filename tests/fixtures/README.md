# Fixture corpus

Synthetic medical-record-shaped documents with known contents, used to measure
per-category scrub recall and precision.

## Provenance rule

**Every value in `corpus/` is invented from scratch.** Nothing here is copied
or adapted from a real patient record, and nothing from a real record may be
quoted into a fixture, a test, a commit message, or any file in this
repository. This repository has a **public** GitHub remote, so a fixture
derived from a real document would be the exact disclosure the scrubber exists
to prevent, self-inflicted.

Real reproduction material stays in the gitignored `local-notes/` directory and
never moves out of it.

Three values are **published reference values** that CMS prints in its own
public format documentation — not patient data:

| Value | What it is |
|---|---|
| `1234567893` | NPI check-digit example, CMS *Requirements for National Provider Identifier (NPI) and NPI Check Digit* |
| `1EG4TE5MK73` | MBI format example, CMS *Understanding the Medicare Beneficiary Identifier* |
| `AB1234563` | DEA registration number with a valid check digit |

Three more are **invented and then made checksum-valid** so a second instance
of each format exists: `1740467307` (NPI), `BP9876547` (DEA), `2FT6WE9NQ85`
(MBI). They were checked against presidio's validators, which rejected the
first two attempts — a useful reminder that these recognizers reject a value
outright rather than scoring it low.

These exist so the checksum-validated recognizers can be tested in both
directions — a valid value detected, an invalid one rejected.

## Layout

- `corpus/*.txt` — the documents
- `corpus/ground_truth.json` — what each document is known to contain, with
  offsets
- `build_ground_truth.py` — regenerates the manifest's offsets from the
  declarations it holds. Run from the repository root after editing a document:

  ```
  python3.12 tests/fixtures/build_ground_truth.py
  ```

`tests/test_scrub_quality.py` asserts every recorded offset still slices out its
recorded value, so the manifest and the text cannot drift apart silently.

## What this corpus cannot test

A corpus invented from scratch contains the identifier shapes its author
anticipated. The de-identification literature consistently reports that the
hardest category is institution-specific formats — local abbreviations,
internal codes, facility-specific layouts — which is precisely what nobody can
invent from memory. A green run here proves the recognizers work on identifiers
we already knew how to describe.

Requirement R17 is the external check: before the tool is relied on for client
work, run it over a real batch held in `local-notes/`, hand-check a sample of
every category, and record the per-category result outside this repository.
