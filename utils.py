import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

__version__ = '0.3.0'

SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.eml', '.msg', '.txt'}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def document_id(path: Path) -> str:
    """Stable across processes, with no cleartext filename in dataset metadata."""
    return 'anon_' + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]


@contextmanager
def atomic_text(path: Path):
    """Replace one file only after its complete private replacement is durable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, data) -> None:
    with atomic_text(path) as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)


def private_append(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
                     'a', encoding='utf-8')


def csv_safe(value):
    """Prevent spreadsheet formula execution without changing private JSON logs."""
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value
