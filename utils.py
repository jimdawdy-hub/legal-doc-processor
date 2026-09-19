import hashlib
from pathlib import Path

SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg', '.txt'}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()


def document_key(path: Path) -> str:
    """A stable identity for a source document (KTD14).

    SHA-256 of the resolved path. Stable across runs and across edits to the
    document's contents, distinct for two files that share a name in different
    client folders, and -- being one-way -- it puts no path component inside
    the output directory (R16).

    Not Python's hash(), which is salted per process: the previous anon_id
    derivation produced a different value on every invocation, and was not the
    eight digits its format string claimed.

    Deliberately the path rather than the file's contents, even though KTD14
    names the file's SHA-256. Identity has to survive a document being
    corrected between runs, or R14's 'replace rather than duplicate' cannot
    hold: a content-keyed record would be orphaned by the very edit that made
    the re-run necessary. The content hash is carried alongside, as the
    change signal rather than the identity.
    """
    return hashlib.sha256(str(path.resolve()).encode('utf-8')).hexdigest()


def anon_id(path: Path) -> str:
    """The only name a source document is known by inside the output directory."""
    return f"anon_{document_key(path)[:12]}"
