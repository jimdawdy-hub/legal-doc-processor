"""
Auto-generates review artifacts after each pipeline run:
  output/summary.html            — interactive run report; make decisions, download CSV
  output/review/review_log.csv   — all PII flags, spreadsheet-ready
"""

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from utils import HOLD_BACK_LABELS
from writer import records_root


def generate_reports(output_dir: Path) -> None:
    review_dir = output_dir / 'review'
    provenance = _load_json(output_dir / 'provenance.json')

    flags = _load_flags(provenance)
    # Written unconditionally. It used to be written only when the flag list
    # was non-empty, so a run that produced no flags left the previous run's
    # CSV sitting there looking current.
    _write_csv(flags, review_dir / 'review_log.csv')

    _write_html(flags, provenance, output_dir / 'summary.html')


def _load_flags(provenance: dict) -> list:
    """Identifier types, counts and locations, read from the evidence record.

    This used to load its whole flag set from output_dir/review/review_log.jsonl
    -- the record's predecessor, which lived inside the output directory and
    carried original values and sixty-character context windows. Relocating
    that record without rewiring this reader would have made the summary
    silently report zero identifiers instead of failing (R10, KTD7).

    Only documents this run actually recorded are read, matched by the
    anonymous id in the provenance manifest.
    """
    known = {f.get('doc_key') for f in provenance.get('files', [])}
    known.discard(None)
    if not known:
        return []

    flags = []
    evidence_dir = records_root() / 'evidence'
    for doc_key in sorted(known):
        record_path = evidence_dir / f'{doc_key}.json'
        if not record_path.exists():
            continue
        try:
            record = json.loads(record_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for entity_type, count in sorted(record.get('counts_by_type', {}).items()):
            flags.append({
                'anon_id': record.get('anon_id', ''),
                'entity_type': entity_type,
                'count': count,
            })
    return flags


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_csv(flags: list, out_path: Path) -> None:
    """Identifier types and counts per document.

    No original_text and no context column: this file is inside the output
    directory, and a sixty-character window around a flagged ZIP routinely
    contains the patient's name (R10, KTD7). No decision column either -- the
    approval workflow it fed is gone.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ['anon_id', 'entity_type', 'count']
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for flag in flags:
            writer.writerow(flag)


def _esc(s: str) -> str:
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _write_html(flags: list, provenance: dict, out_path: Path) -> None:
    """A read-only run report.

    No decision controls, no fetch calls, and no identifier values or context
    excerpts. The page renders standalone with no server running -- the
    approval workflow it used to drive is gone, and the values it used to show
    are the ones R10 keeps out of the output directory.
    """
    summary = provenance.get('summary', {})
    files = provenance.get('files', [])
    dataset_name = _esc(provenance.get('dataset_name') or 'Legal Document Dataset')
    generated = datetime.now().strftime('%Y-%m-%d %H:%M')

    held_back = summary.get('held_back') or {
        'ocr_confidence_low': summary.get('review_queue_files', 0)
    }
    hold_back_tiles = '\n  '.join(
        f'<div class="stat"><div class="val">{held_back.get(reason, 0)}</div>'
        f'<div class="lbl">{_esc(label)}</div></div>'
        for reason, label in HOLD_BACK_LABELS.items()
    )

    by_type = Counter()
    by_doc = defaultdict(Counter)
    for flag in flags:
        by_type[flag['entity_type']] += flag['count']
        by_doc[flag['anon_id']][flag['entity_type']] += flag['count']
    total_identifiers = sum(by_type.values())

    file_rows = ''
    for rec in sorted(files, key=lambda r: (r.get('doc_type', ''),
                                            r.get('anon_id', r.get('doc_key', '')))):
        processing = rec.get('processing', {})
        doc_type = rec.get('doc_type', '').lower()
        tag_cls = (f"tag-{doc_type}"
                   if doc_type in ('private', 'caselaw', 'published', 'uncertain')
                   else 'tag')
        label = _esc(rec.get('anon_id') or (rec.get('doc_key') or '')[:12] or '-')
        if rec.get('skipped'):
            reason = rec.get('skip_reason', '')
            file_rows += (
                f'<tr><td class="skip">{label}</td>'
                f'<td class="skip" colspan="5">HELD BACK &mdash; '
                f'{_esc(HOLD_BACK_LABELS.get(reason, reason))}</td></tr>\n'
            )
        else:
            flag_cnt = processing.get('review_flags', 0)
            file_rows += (
                f'<tr><td>{label}</td>'
                f'<td><span class="tag {tag_cls}">{_esc(doc_type)}</span></td>'
                f'<td>{processing.get("token_count", 0):,}</td>'
                f'<td>{flag_cnt or "&mdash;"}</td>'
                f'<td>{processing.get("chunk_count", 0)}</td>'
                f'<td>{"yes" if processing.get("ocr") else "&mdash;"}</td></tr>\n'
            )

    type_rows = ''
    for entity_type, count in by_type.most_common():
        share = 100 * count / total_identifiers if total_identifiers else 0
        type_rows += (
            f'<tr><td>{_esc(entity_type)}</td><td>{count:,}</td>'
            f'<td>{share:.1f}%</td></tr>\n'
        )

    doc_rows = ''
    for label, types in sorted(by_doc.items(), key=lambda kv: -sum(kv[1].values())):
        detail = ', '.join(f"{t}: {n}" for t, n in types.most_common(5))
        doc_rows += (
            f'<tr><td>{_esc(label)}</td><td>{sum(types.values()):,}</td>'
            f'<td>{_esc(detail)}</td></tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pipeline Report &mdash; {dataset_name}</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --muted: #555; --line: #e2e2e2;
    --panel: #f7f7f8; --accent: #1f4e79;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #16171a; --fg: #ececec; --muted: #a0a0a0; --line: #303237;
      --panel: #1e2024; --accent: #7fb3e0;
    }}
  }}
  body {{
    background: var(--bg); color: var(--fg); margin: 0 auto; padding: 24px 16px;
    max-width: 1000px;
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 10px; color: var(--accent); }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 20px; }}
  .stat-grid {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }}
  .stat {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
    padding: 10px 14px; min-width: 130px; flex: 1 1 130px;
  }}
  .stat .val {{ font-size: 20px; font-weight: 600; }}
  .stat .lbl {{ font-size: 11px; color: var(--muted); }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 11px;
        text-transform: uppercase; letter-spacing: .04em; }}
  td.skip {{ color: var(--muted); font-style: italic; }}
  .tag {{ display: inline-block; padding: 1px 7px; border-radius: 10px;
          font-size: 11px; background: var(--panel); border: 1px solid var(--line); }}
  .note {{ background: var(--panel); border: 1px solid var(--line);
           border-left: 3px solid var(--accent); border-radius: 4px;
           padding: 10px 14px; font-size: 12px; color: var(--muted); }}
</style>
</head>
<body>
<h1>Pipeline Report &mdash; {dataset_name}</h1>
<div class="meta">Generated {generated} &nbsp;&middot;&nbsp;
  {summary.get('total_files', 0)} files &nbsp;&middot;&nbsp;
  {summary.get('total_tokens', 0):,} tokens
</div>

<h2>Run Summary</h2>
<div class="stat-grid">
  <div class="stat"><div class="val">{summary.get('total_files', 0)}</div><div class="lbl">Files processed</div></div>
  <div class="stat"><div class="val">{summary.get('total_tokens', 0):,}</div><div class="lbl">Total tokens</div></div>
  <div class="stat"><div class="val">{total_identifiers:,}</div><div class="lbl">Identifiers found</div></div>
</div>
<div class="stat-grid">
  {hold_back_tiles}
</div>
<div class="stat-grid">
  <div class="stat"><div class="val">{summary.get('caselaw_files', 0)}</div><div class="lbl">Case law to RAG</div></div>
  <div class="stat"><div class="val">{summary.get('private_files', 0)}</div><div class="lbl">Private to finetune</div></div>
  <div class="stat"><div class="val">{summary.get('uncertain_files', 0)}</div><div class="lbl">Uncertain to finetune</div></div>
  <div class="stat"><div class="val">{summary.get('published_files', 0)}</div><div class="lbl">Published to RAG+finetune</div></div>
</div>

<div class="note">
  Documents are listed by their anonymous id. This report carries identifier
  <strong>types, counts and locations</strong> and never the identifiers
  themselves &mdash; original values live only in the verification record,
  outside this directory, and are deleted when the batch is accepted.
</div>

<h2>Per-Document Results</h2>
<table>
  <tr><th>Document</th><th>Type</th><th>Tokens</th><th>Ambiguous</th><th>Chunks</th><th>OCR</th></tr>
  {file_rows}
</table>

<h2>Identifiers by Type</h2>
<table>
  <tr><th>Type</th><th>Count</th><th>Share</th></tr>
  {type_rows or '<tr><td colspan="3">No identifiers recorded.</td></tr>'}
</table>

<h2>Identifiers by Document</h2>
<table>
  <tr><th>Document</th><th>Count</th><th>Types</th></tr>
  {doc_rows or '<tr><td colspan="3">No identifiers recorded.</td></tr>'}
</table>

</body>
</html>
"""
    out_path.write_text(html, encoding='utf-8')
