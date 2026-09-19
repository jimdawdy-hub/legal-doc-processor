import pytest
from pathlib import Path
import tempfile

CASELAW_TEXT = """
UNITED STATES COURT OF APPEALS FOR THE SEVENTH CIRCUIT

Smith v. Jones, 123 F.3d 456 (7th Cir. 2019)

OPINION

The court held that the district court did not abuse its discretion.
The judgment is AFFIRMED.

I. BACKGROUND

The plaintiff filed suit alleging breach of contract. The district court
granted summary judgment in favor of defendant.

II. ANALYSIS

We review the district court's grant of summary judgment de novo.

A. Standard of Review

Summary judgment is appropriate where there is no genuine issue of
material fact. Fed. R. Civ. P. 56(a).

III. CONCLUSION

For the foregoing reasons, the judgment of the district court is AFFIRMED.
"""

PUBLISHED_TEXT = """
Chicago Bar Journal
Vol. 42, No. 3 — ISSN 0009-3157

Continuing Legal Education — Advanced Evidence

By Professor Jane Williams, University of Chicago Law School
© 2023 Chicago Bar Association

This article examines the evolution of hearsay exceptions under FRE 803.
"""

PRIVATE_TEXT = """
From: james.kowalski@lawfirm.com
To: sarah.chen@client.com
Subject: Case Update — Confidential
CC: tom.bradley@lawfirm.com
Date: March 3, 2024

Dear Sarah,

I am writing to update you on the status of your case.

LAW DIVISION — Cook County Circuit Court

ATTORNEYS FOR PLAINTIFF: James Kowalski, Sarah Chen
ATTORNEYS FOR DEFENDANT: Robert Mills

Plaintiff Jane Doe (SSN: 123-45-6789) resides at 123 Main Street, Chicago, IL 60601.
Her phone number is (312) 555-0100.

Sincerely,
James Kowalski
"""

# --- Medical record fixtures -------------------------------------------------
# Invented from scratch. Nothing below is derived from, or adapted from, a real
# patient record: this repository has a public remote (plan Q3), so every value
# here is made up and must stay that way.

DISCHARGE_SSN = "412-55-9083"
DISCHARGE_MRN = "4417392"
DISCHARGE_NAME = "Harold Vance"
DISCHARGE_DOB = "03/14/1951"
DISCHARGE_ADDRESS = "88 Larkspur Lane"
DISCHARGE_ZIP = "60655"
DISCHARGE_PHONE = "(312) 555-0147"

DISCHARGE_SUMMARY_BASE = f"""MERCY GENERAL HOSPITAL
Discharge Summary

Patient Name: {DISCHARGE_NAME}
Medical Record Number: {DISCHARGE_MRN}
Date of Birth: {DISCHARGE_DOB}
Social Security Number: {DISCHARGE_SSN}
Address: {DISCHARGE_ADDRESS}, Riverton, IL {DISCHARGE_ZIP}
Phone: {DISCHARGE_PHONE}

Admitted 08/02/2025 for elective knee arthroplasty. Recovery was uneventful
and the surgical site remained clean and dry throughout the stay.

Discharged home 08/05/2025 with physical therapy follow-up in two weeks.
"""

# Each of these lines, on its own, is enough to classify the record above as a
# published document -- which used to route it around the scrubber entirely.
# All three are ordinary furniture on real medical paperwork.
PUBLISHED_LOOKALIKE_FOOTERS = {
    'copyright': "© 2025 Mercy General Hospital",
    'bar_association': "Health fair co-hosted with the Cook County Bar Association.",
    'issn': "Patient Education Series - ISSN: 1234-5678",
}


def discharge_summary(footer: str = 'copyright') -> str:
    """A synthetic discharge summary carrying one published-lookalike line."""
    return f"{DISCHARGE_SUMMARY_BASE}\n{PUBLISHED_LOOKALIKE_FOOTERS[footer]}\n"


def assert_lines_preserved(source: str, scrubbed: str) -> None:
    """Every line entering strip_pii is represented in its output.

    Scoped to the scrub step on purpose. cleaner.clean() deliberately deletes
    whole lines before this point -- publisher footers, thrice-repeated running
    headers, blank runs -- so the same assertion made end to end would be false
    and would fail over a mis-scoped test rather than a real defect.
    """
    src_lines = source.split('\n')
    out_lines = scrubbed.split('\n')
    assert len(out_lines) == len(src_lines), (
        f"line count changed: {len(src_lines)} in, {len(out_lines)} out -- "
        f"a span ran past a line break and swallowed the next line"
    )
    for i, (before, after) in enumerate(zip(src_lines, out_lines)):
        if before.strip():
            assert after.strip(), f"line {i} was emptied: {before!r}"


@pytest.fixture
def lines_preserved():
    return assert_lines_preserved


@pytest.fixture
def discharge_text():
    """Factory: discharge_text(footer) -> a synthetic discharge summary."""
    return discharge_summary


@pytest.fixture
def discharge_ids():
    """The identifiers planted in the discharge-summary fixture."""
    return {
        'name': DISCHARGE_NAME,
        'mrn': DISCHARGE_MRN,
        'dob': DISCHARGE_DOB,
        'ssn': DISCHARGE_SSN,
        'address': DISCHARGE_ADDRESS,
        'zip': DISCHARGE_ZIP,
        'phone': DISCHARGE_PHONE,
    }


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)

@pytest.fixture
def caselaw_text():
    return CASELAW_TEXT

@pytest.fixture
def published_text():
    return PUBLISHED_TEXT

@pytest.fixture
def private_text():
    return PRIVATE_TEXT
