import hashlib
from pathlib import Path

SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg', '.txt'}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()
