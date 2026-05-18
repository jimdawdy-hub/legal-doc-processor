#!/usr/bin/env python3
"""
Second-pass redaction for the legal-doc-processor pipeline.

Reads re_id_risk_report.json (produced by re_id_risk.py), extracts quasi-identifiers
and additional_redactions flagged by Claude, and applies them to all JSONL output files.

Usage:
    python3.12 second_pass.py --output /path/to/output [--dry-run] [--blocklist terms.txt]

--dry-run   Show what would be redacted without modifying files.
--blocklist Path to a plain-text file with one extra term per line to always redact.

Output:
    Patches output/finetune/dataset.jsonl and output/rag/*.jsonl in-place.
    Appends a 'second_pass_redaction' entry to provenance.json.
    Regenerates summary.html.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reporter import generate_reports

# ---------------------------------------------------------------------------
# Replacement token map: quasi-identifier type → [TOKEN]
# Types that map to the same token are grouped together.
# ---------------------------------------------------------------------------

_TYPE_TOKEN: dict[str, str] = {}

def _register(*types: str, token: str) -> None:
    for t in types:
        _TYPE_TOKEN[t.lower()] = token

_register('patient_name', 'patient_name_alt', 'full_name', 'name', 'plaintiff_name',
          'plaintiff', 'patient_surname', 'surname_in_text', 'mother_name', 'parent',
          'treating_physician', 'crew_names', 'crew_names_and_licenses',
          'clinician names', 'deponent_details', 'individual_defendant',
          'attorney_names_and_firms', 'clerk_name',
          token='[PERSON]')

_register('facility', 'facility_name', 'facility_pair', 'facility/address',
          'facility identifiers', 'facility_code', 'hospital_facility',
          'defendant_entity', 'defendant_entities', 'defendant_name',
          'provider_name', 'provider/company',
          'ambulance_company_and_run', 'requester address',
          token='[ORGANIZATION]')

_register('case_number', 'docket_number', 'court/docket', 'case_caption',
          'case caption', 'case_caption_marker', 'exhibit_marking',
          'court', 'court/venue', 'court_venue',
          token='[CASE_NO]')

_register('date_of_birth', 'dob', 'date of request', 'date_signed',
          'date_range', 'date_of_service', 'incident_date', 'incident_date_time',
          'filing_date',
          token='[DATE]')

_register('address', 'home_address', 'geographic anchor', 'geographic_anchor',
          'geography', 'address_in_clinical_note', 'facility/address',
          token='[ADDRESS]')

_register('mrn', 'medical_record_number', 'fin/account', 'account_numbers',
          'incident_number', 'firm_number', 'ardc_number', 'anesthesiologist id',
          'anesthesiologist_id',
          token='[ID_NUMBER]')

_register('ssn', 'ssn_fragment', 'credit_card_holder',
          token='[SSN]')

_register('email', token='[EMAIL]')

_register('citation', token='[CITATION]')

_FALLBACK_TOKEN = '[REDACTED]'


def _token_for_type(qi_type: str) -> str:
    return _TYPE_TOKEN.get(qi_type.lower().strip(), _FALLBACK_TOKEN)


# ---------------------------------------------------------------------------
# Extract redaction targets from the risk report
# ---------------------------------------------------------------------------

def _extract_targets(report: dict) -> list[tuple[str, str]]:
    """
    Returns list of (raw_value, replacement_token) pairs.
    Values come from quasi_identifiers and additional_redactions in every assessment.
    """
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(value: str, token: str) -> None:
        value = value.strip()
        if not value or value.lower() in seen or len(value) > 300:
            return
        # Skip values that are pure descriptions rather than extractable strings
        if value.startswith('(') or '→' in value or value.startswith('e.g.'):
            return
        # Skip bare 4-digit years — too broad, will match legitimate date references
        if re.fullmatch(r'\d{4}', value):
            return
        seen.add(value.lower())
        targets.append((value, token))

    for assessment in report.get('assessments', []):
        for qi in assessment.get('quasi_identifiers', []):
            raw = qi.get('value', '').strip()
            token = _token_for_type(qi.get('type', ''))
            # Values often contain notes after a slash or parenthetical — take first segment
            for part in re.split(r'\s*[/;]\s*|\s*\(also\s*', raw):
                part = part.strip().rstrip(')')
                if len(part) >= 3:
                    _add(part, token)

        for phrase in assessment.get('additional_redactions', []):
            _add(phrase, _FALLBACK_TOKEN)

    # Sort longest first so longer matches take precedence over sub-strings
    targets.sort(key=lambda t: -len(t[0]))
    return targets


def _load_blocklist(path: Path) -> list[tuple[str, str]]:
    targets = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split('\t', 1)
                value = parts[0].strip()
                token = parts[1].strip() if len(parts) > 1 else _FALLBACK_TOKEN
                if value:
                    targets.append((value, token))
    targets.sort(key=lambda t: -len(t[0]))
    return targets


# ---------------------------------------------------------------------------
# Compile regex patterns
# ---------------------------------------------------------------------------

def _compile_patterns(targets: list[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    patterns = []
    for value, token in targets:
        escaped = re.escape(value)
        # Word-boundary anchors where value starts/ends with word chars
        lb = r'\b' if re.match(r'^\w', value) else ''
        rb = r'\b' if re.search(r'\w$', value) else ''
        try:
            pat = re.compile(lb + escaped + rb, re.IGNORECASE)
            patterns.append((pat, token))
        except re.error:
            pass
    return patterns


def _apply_patterns(text: str, patterns: list[tuple[re.Pattern, str]]) -> tuple[str, int]:
    count = 0
    for pat, token in patterns:
        new, n = pat.subn(token, text)
        if n:
            text = new
            count += n
    return text, count


# ---------------------------------------------------------------------------
# Process JSONL files
# ---------------------------------------------------------------------------

def _patch_jsonl(path: Path, patterns: list[tuple[re.Pattern, str]],
                 dry_run: bool, verbose: bool) -> dict:
    """Patch all 'text' fields in a JSONL file. Returns stats."""
    records = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    total_subs = 0
    changed = 0
    for rec in records:
        text = rec.get('text', '')
        new_text, n = _apply_patterns(text, patterns)
        if n:
            total_subs += n
            changed += 1
            if not dry_run:
                rec['text'] = new_text

    if not dry_run and total_subs:
        with open(path, 'w', encoding='utf-8') as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    rel = path.name
    if verbose and total_subs:
        print(f"    {rel}: {total_subs} substitutions in {changed}/{len(records)} records")
    return {'file': str(path), 'records': len(records), 'records_changed': changed,
            'substitutions': total_subs}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_second_pass(
    output_dir: Path,
    extra_blocklist: list[tuple[str, str]] | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> dict:
    report_path = output_dir / 're_id_risk_report.json'
    if not report_path.exists():
        print(f"No re_id_risk_report.json found. Run re_id_risk.py first.", file=sys.stderr)
        sys.exit(1)

    report = json.loads(report_path.read_text())
    targets = _extract_targets(report)
    if extra_blocklist:
        # Prepend (already sorted by length) — merge and re-sort
        all_targets = extra_blocklist + targets
        all_targets.sort(key=lambda t: -len(t[0]))
    else:
        all_targets = targets

    patterns = _compile_patterns(all_targets)

    if not quiet:
        prefix = '[DRY RUN] ' if dry_run else ''
        print(f"\n{prefix}Second-Pass Redaction")
        print(f"  Risk report:  {report_path}")
        print(f"  Targets:      {len(all_targets)} terms / {len(patterns)} patterns")
        print(f"  Files:        {'(not writing)' if dry_run else output_dir}\n")

    # Collect JSONL files to process
    jsonl_files: list[Path] = []
    ft = output_dir / 'finetune' / 'dataset.jsonl'
    if ft.exists():
        jsonl_files.append(ft)
    for p in sorted((output_dir / 'rag').glob('*.jsonl')):
        jsonl_files.append(p)

    if not jsonl_files:
        print("No JSONL output files found.", file=sys.stderr)
        sys.exit(1)

    file_stats = []
    total_subs = 0
    for p in jsonl_files:
        stats = _patch_jsonl(p, patterns, dry_run=dry_run, verbose=not quiet)
        file_stats.append(stats)
        total_subs += stats['substitutions']
        if not quiet and stats['substitutions']:
            pass  # already printed inside _patch_jsonl

    if not quiet:
        print(f"  Total substitutions: {total_subs} across {len(jsonl_files)} file(s)")
        if dry_run:
            print("  (Dry run — no files modified)")

    if not dry_run:
        _update_provenance(output_dir, all_targets, file_stats, total_subs)
        generate_reports(output_dir)
        if not quiet:
            print(f"  Provenance updated, reports regenerated.")

    return {
        'dry_run': dry_run,
        'patterns': len(patterns),
        'total_substitutions': total_subs,
        'files': file_stats,
    }


def _update_provenance(output_dir: Path, targets: list[tuple[str, str]],
                       file_stats: list[dict], total_subs: int) -> None:
    prov_path = output_dir / 'provenance.json'
    if not prov_path.exists():
        return
    prov = json.loads(prov_path.read_text())
    prov.setdefault('second_pass_redactions', []).append({
        'applied_at': datetime.now(timezone.utc).isoformat(),
        'terms_applied': len(targets),
        'total_substitutions': total_subs,
        'files_patched': [s['file'] for s in file_stats if s['substitutions']],
    })
    prov_path.write_text(json.dumps(prov, indent=2))


# ---------------------------------------------------------------------------
# Preview helper — show sample matches before committing
# ---------------------------------------------------------------------------

def preview_matches(output_dir: Path, patterns: list[tuple[re.Pattern, str]],
                    max_examples: int = 5) -> None:
    ft = output_dir / 'finetune' / 'dataset.jsonl'
    if not ft.exists():
        return

    hits: dict[str, list[str]] = defaultdict(list)
    with open(ft, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            text = rec.get('text', '')
            for pat, token in patterns:
                for m in pat.finditer(text):
                    if len(hits[token]) < max_examples:
                        start = max(0, m.start() - 30)
                        end = min(len(text), m.end() + 30)
                        ctx = '…' + text[start:end].replace('\n', ' ') + '…'
                        hits[token].append(f"  '{m.group()}' → {token}  [{ctx}]")

    if hits:
        print("\nSample matches (first 5 per token):")
        for token, examples in sorted(hits.items()):
            print(f"\n  {token}:")
            for ex in examples[:max_examples]:
                print(f"    {ex}")
    else:
        print("\n  (No matches found in finetune dataset)")


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Second-pass redaction using re_id_risk_report.json as the target list.'
    )
    parser.add_argument('--output', required=True, type=Path,
                        help='Pipeline output directory')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show match counts without modifying files')
    parser.add_argument('--preview', action='store_true',
                        help='Show sample text matches before committing (implies --dry-run)')
    parser.add_argument('--blocklist', type=Path,
                        help='Plain-text file with extra terms to redact (one per line; '
                             'optionally TAB-separated with a [TOKEN] in column 2)')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    if args.preview:
        args.dry_run = True

    extra: list[tuple[str, str]] = []
    if args.blocklist:
        extra = _load_blocklist(args.blocklist)
        print(f"Loaded {len(extra)} extra terms from {args.blocklist}")

    result = run_second_pass(
        args.output,
        extra_blocklist=extra or None,
        dry_run=args.dry_run,
        quiet=args.quiet,
    )

    if args.preview:
        report_path = args.output / 're_id_risk_report.json'
        report = json.loads(report_path.read_text())
        targets = _extract_targets(report)
        if extra:
            targets = extra + targets
            targets.sort(key=lambda t: -len(t[0]))
        patterns = _compile_patterns(targets)
        preview_matches(args.output, patterns)

    total = result['total_substitutions']
    if args.dry_run:
        print(f"\nDry run complete — {total} substitutions would be made across "
              f"{len(result['files'])} file(s).")
    else:
        print(f"\nDone — {total} substitutions applied.")


if __name__ == '__main__':
    main()
