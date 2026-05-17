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
