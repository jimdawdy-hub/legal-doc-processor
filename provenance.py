import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils import anon_id, document_key, sha256_file, HOLD_BACK_LABELS


class ProvenanceManifest:
    def __init__(self, output_dir: Path, sidecar_path: Optional[Path] = None,
                 source_dir: Optional[Path] = None):
        self.output_dir = output_dir
        self.manifest_path = output_dir / 'provenance.json'
        self.sidecar = self._load_sidecar(sidecar_path) if sidecar_path else {}
        self._files: list = []
        self._source_dir = str(source_dir.resolve()) if source_dir else None
        self._dataset_meta = self._load_or_init_dataset_meta()

    def _load_sidecar(self, path: Path) -> dict:
        result = {}
        with open(path, newline='') as f:
            for row in csv.DictReader(f):
                result[row['filename']] = {
                    'copyright_status': row.get('copyright_status', ''),
                    'source_url_or_collection': row.get('source_url_or_collection', ''),
                    'notes': row.get('notes', ''),
                }
        return result

    def _load_or_init_dataset_meta(self) -> dict:
        fields = ('dataset_name', 'version', 'created', 'created_by',
                  'source_collection', 'license', 'jurisdiction_coverage', 'notes')
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text())
            return {k: existing[k] for k in fields if k in existing}
        return {
            'dataset_name': '',
            'version': '1.0.0',
            'created': datetime.now(timezone.utc).date().isoformat(),
            'created_by': '',
            'source_collection': '',
            'license': '',
            'jurisdiction_coverage': [],
            'notes': '',
        }

    def add_file(
        self,
        original_path: Path,
        doc_type: str,
        classification_confidence: float,
        ocr: bool,
        ocr_confidence: Optional[float],
        pii_stripped: bool,
        faker_substitutions: int,
        review_flags: int,
        chunk_count: int,
        token_count: int,
        skipped: bool = False,
        skip_reason: Optional[str] = None,
    ) -> None:
        sidecar_info = self.sidecar.get(original_path.name, {})
        # R16: no filename, no path, no folder name. This manifest is inside
        # the output directory, so a client folder named for the patient used
        # to reach the deliverable verbatim. The id-to-filename mapping lives
        # in the evidence record instead, outside that directory.
        self._files.append({
            'doc_key': document_key(original_path),
            'anon_id': anon_id(original_path),
            'sha256': sha256_file(original_path),
            'doc_type': doc_type,
            'classification_confidence': round(classification_confidence, 3),
            'date_processed': datetime.now(timezone.utc).isoformat(),
            'copyright_status': sidecar_info.get('copyright_status', ''),
            'source_url_or_collection': sidecar_info.get('source_url_or_collection', ''),
            'notes': sidecar_info.get('notes', ''),
            'skipped': skipped,
            'skip_reason': skip_reason,
            'processing': {
                'ocr': ocr,
                'ocr_confidence': round(ocr_confidence, 1) if ocr_confidence is not None else None,
                'pii_stripped': pii_stripped,
                'faker_substitutions': faker_substitutions,
                'review_flags': review_flags,
                'chunk_count': chunk_count,
                'token_count': token_count,
            },
        })

    def _merged_files(self) -> list:
        """This run's entries merged over any earlier run's (R14).

        The file list used to be replaced wholesale, so a second run over a
        smaller input directory erased every record of the documents processed
        before it. Entries are keyed by doc_key: this run replaces what it
        touched and leaves the rest intact.
        """
        if not self.manifest_path.exists():
            return list(self._files)
        try:
            previous = json.loads(self.manifest_path.read_text()).get('files', [])
        except (json.JSONDecodeError, OSError):
            return list(self._files)

        merged, order = {}, []
        for entry in previous + self._files:
            key = entry.get('doc_key') or entry.get('anon_id')
            if key not in merged:
                order.append(key)
            merged[key] = entry
        return [merged[k] for k in order]

    def save(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._files = self._merged_files()
        processed = [f for f in self._files if not f['skipped']]
        summary = {
            'total_files': len(self._files),
            'processed_files': len(processed),
            'skipped_files': len(self._files) - len(processed),
            'total_tokens': sum(f['processing']['token_count'] for f in processed),
            'caselaw_files': sum(1 for f in processed if f['doc_type'] == 'caselaw'),
            'published_files': sum(1 for f in processed if f['doc_type'] == 'published'),
            'private_files': sum(1 for f in processed if f['doc_type'] == 'private'),
            'uncertain_files': sum(1 for f in processed if f['doc_type'] == 'uncertain'),
            'ocr_files': sum(1 for f in processed if f['processing']['ocr']),
            # One counter per hold-back reason. This used to count only the
            # OCR reason, so any other hold-back was invisible in the summary.
            'held_back': {
                reason: sum(1 for f in self._files if f.get('skip_reason') == reason)
                for reason in HOLD_BACK_LABELS
            },
            # Kept under its original name for the OCR tile and for anything
            # already reading it.
            'review_queue_files': sum(
                1 for f in self._files if f.get('skip_reason') == 'ocr_confidence_low'
            ),
            'pii_review_flags': sum(f['processing']['review_flags'] for f in processed),
        }
        # No manifest-level source_dir either: it held the resolved input
        # directory, which is the third and most easily missed way a client
        # folder name reached the deliverable (R16). It is recorded in the
        # evidence record, outside this directory.
        manifest = {**self._dataset_meta, 'files': self._files, 'summary': summary}
        self.manifest_path.write_text(json.dumps(manifest, indent=2))
