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


def generate_reports(output_dir: Path) -> None:
    review_dir = output_dir / 'review'
    review_log = review_dir / 'review_log.jsonl'
    provenance_path = output_dir / 'provenance.json'

    flags = _load_flags(review_log)
    provenance = _load_json(provenance_path)

    if flags:
        _write_csv(flags, review_dir / 'review_log.csv')

    _write_html(flags, provenance, output_dir / 'summary.html')


def _load_flags(path: Path) -> list:
    if not path.exists():
        return []
    flags = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                flags.append(json.loads(line))
    return flags


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_csv(flags: list, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ['flag_id', 'file', 'entity_type', 'confidence',
                  'original_text', 'context', 'decision']
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for i, flag in enumerate(flags):
            row = dict(flag)
            row['flag_id'] = i
            row['decision'] = 'approve'
            writer.writerow(row)


def _esc(s: str) -> str:
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _write_html(flags: list, provenance: dict, out_path: Path) -> None:
    summary = provenance.get('summary', {})
    files = provenance.get('files', [])
    dataset_name = _esc(provenance.get('dataset_name') or 'Legal Document Dataset')
    generated = datetime.now().strftime('%Y-%m-%d %H:%M')

    by_type = Counter(f['entity_type'] for f in flags)
    by_file = defaultdict(list)
    for f in flags:
        by_file[f['file']].append(f)

    # Embed flags as JSON for JavaScript
    flags_json = json.dumps([
        {'id': i, 'file': f['file'], 'entity_type': f['entity_type'],
         'confidence': f['confidence'], 'original_text': f['original_text'],
         'context': f['context']}
        for i, f in enumerate(flags)
    ])
    # Index of file names matching the data-bfile-idx on batch buttons
    file_index_json = json.dumps(
        [fname for fname, _ in sorted(by_file.items(), key=lambda x: -len(x[1]))]
    )

    # --- per-file table rows ---
    file_rows = ''
    for rec in sorted(files, key=lambda r: r.get('doc_type', '') + r.get('original_filename', '')):
        p = rec.get('processing', {})
        doc_type = rec.get('doc_type', '').lower()
        tag_cls = f"tag-{doc_type}" if doc_type in ('private', 'caselaw', 'published', 'uncertain') else 'tag'
        ocr_str = '✓' if p.get('ocr') else '—'
        flag_cnt = p.get('review_flags', 0)
        flag_str = f'<span class="tag tag-pii">{flag_cnt}</span>' if flag_cnt else '—'
        if rec.get('skipped'):
            file_rows += f'<tr><td class="skip">{_esc(rec["original_filename"])}</td><td class="skip" colspan="5">SKIPPED — {_esc(rec.get("skip_reason",""))}</td></tr>\n'
        else:
            file_rows += (
                f'<tr><td>{_esc(rec["original_filename"])}</td>'
                f'<td><span class="tag {tag_cls}">{doc_type}</span></td>'
                f'<td>{p.get("token_count",0):,}</td>'
                f'<td>{flag_str}</td>'
                f'<td>{p.get("chunk_count",0)}</td>'
                f'<td>{ocr_str}</td></tr>\n'
            )

    # --- entity type summary rows ---
    type_rows = ''
    for etype, count in by_type.most_common():
        pct = 100 * count / len(flags) if flags else 0
        type_rows += (
            f'<tr><td>{_esc(etype)}</td><td>{count:,}</td><td>{pct:.1f}%</td>'
            f'<td>'
            f'<button class="batch-btn" data-btype="{_esc(etype)}" data-dec="approve" onclick="batchTypeBtn(this)">All correct</button> '
            f'<button class="batch-btn restore" data-btype="{_esc(etype)}" data-dec="restore" onclick="batchTypeBtn(this)">All wrong</button>'
            f'</td></tr>\n'
        )

    # --- by-file summary rows ---
    # Use data- attributes instead of inline onclick strings to avoid all escaping issues
    file_summary_rows = ''
    for fi, (fname, fflags) in enumerate(sorted(by_file.items(), key=lambda x: -len(x[1]))):
        types = Counter(f['entity_type'] for f in fflags)
        type_str = ', '.join(f"{t}: {n}" for t, n in types.most_common(4))
        file_summary_rows += (
            f'<tr><td>{_esc(fname)}</td><td>{len(fflags):,}</td><td>{_esc(type_str)}</td>'
            f'<td>'
            f'<button class="batch-btn" data-bfile-idx="{fi}" data-dec="approve" onclick="batchFileBtn(this)">All correct</button> '
            f'<button class="batch-btn restore" data-bfile-idx="{fi}" data-dec="restore" onclick="batchFileBtn(this)">All wrong</button>'
            f'</td></tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Pipeline Report — {dataset_name}</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       font-size:13px;color:#222;margin:0;padding:24px 40px 80px}}
  h1{{font-size:20px;margin-bottom:4px}}
  h2{{font-size:14px;font-weight:600;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
  .meta{{color:#666;font-size:12px;margin-bottom:24px}}
  .stat-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}}
  .stat{{background:#f5f5f5;border-radius:6px;padding:12px 16px}}
  .stat .val{{font-size:22px;font-weight:700;color:#1a1a1a}}
  .stat .lbl{{font-size:11px;color:#666;margin-top:2px}}
  table{{border-collapse:collapse;width:100%;margin-bottom:24px}}
  th{{background:#f0f0f0;text-align:left;padding:6px 10px;font-size:11px;
      text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #ddd}}
  td{{padding:5px 10px;border-bottom:1px solid #eee;vertical-align:top}}
  tr:hover td{{background:#fafafa}}
  .tag{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:600}}
  .tag-private{{background:#fee2e2;color:#991b1b}}
  .tag-caselaw{{background:#dbeafe;color:#1e40af}}
  .tag-published{{background:#d1fae5;color:#065f46}}
  .tag-uncertain{{background:#fef3c7;color:#92400e}}
  .tag-pii{{background:#ede9fe;color:#5b21b6}}
  .conf{{color:#888;font-size:11px}}
  .ctx{{font-size:11px;color:#555;font-style:italic;max-width:440px}}
  .skip{{color:#999;font-size:11px}}
  .batch-btn{{font-size:11px;padding:2px 8px;border:1px solid #ccc;border-radius:3px;
              background:#f0fdf4;color:#166534;cursor:pointer}}
  .batch-btn.restore{{background:#fff1f2;color:#991b1b}}
  /* Decision toggle buttons */
  .dec-btn{{font-size:11px;padding:3px 10px;border:none;border-radius:4px;cursor:pointer;
            font-weight:600;transition:all .15s}}
  .dec-approve{{background:#dcfce7;color:#166534;border:1px solid #86efac}}
  .dec-restore{{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}}
  /* Sticky review toolbar */
  #toolbar{{position:fixed;bottom:0;left:0;right:0;background:#1e293b;color:#f1f5f9;
            padding:10px 40px;display:flex;align-items:center;gap:16px;z-index:100;
            font-size:13px;box-shadow:0 -2px 8px rgba(0,0,0,.2)}}
  #toolbar .tl-stat{{color:#94a3b8;font-size:12px}}
  #toolbar .tl-stat span{{color:#f1f5f9;font-weight:600}}
  #dl-btn{{background:#3b82f6;color:#fff;border:none;border-radius:5px;
           padding:7px 18px;font-size:13px;font-weight:600;cursor:pointer}}
  #dl-btn:hover{{background:#2563eb}}
  #reviewer-name{{background:#334155;border:1px solid #475569;border-radius:4px;
                  color:#f1f5f9;padding:6px 10px;font-size:12px;width:200px}}
  #reviewer-name::placeholder{{color:#94a3b8}}
  #apply-btn{{display:none;background:#16a34a;color:#fff;border:none;border-radius:5px;
              padding:7px 18px;font-size:13px;font-weight:600;cursor:pointer;margin-left:auto}}
  #apply-btn:hover{{background:#15803d}}
  #apply-btn:disabled{{background:#64748b;cursor:not-allowed}}
  .result-banner{{display:none;position:fixed;top:0;left:0;right:0;background:#16a34a;
                  color:#fff;padding:14px 40px;font-size:14px;font-weight:600;
                  z-index:200;text-align:center}}
  #filter-bar{{display:flex;gap:8px;margin-bottom:12px;align-items:center}}
  #filter-bar input,#filter-bar select{{padding:5px 8px;border:1px solid #ddd;border-radius:4px;font-size:12px}}
  @media print{{#toolbar,.batch-btn,.dec-btn,.no-print{{display:none!important}}
    body{{padding:12px}}}}
</style>
</head>
<body>
<h1>Pipeline Report — {dataset_name}</h1>
<div class="meta">Generated {generated} &nbsp;·&nbsp;
  {summary.get('total_files', 0)} files processed &nbsp;·&nbsp;
  {summary.get('total_tokens', 0):,} tokens
</div>

<h2>Run Summary</h2>
<div class="stat-grid">
  <div class="stat"><div class="val">{summary.get('total_files',0)}</div><div class="lbl">Files processed</div></div>
  <div class="stat"><div class="val">{summary.get('total_tokens',0):,}</div><div class="lbl">Total tokens</div></div>
  <div class="stat"><div class="val">{summary.get('pii_review_flags',0):,}</div><div class="lbl">PII flags for review</div></div>
  <div class="stat"><div class="val">{summary.get('review_queue_files',0)}</div><div class="lbl">OCR queue (low confidence)</div></div>
</div>
<div class="stat-grid">
  <div class="stat"><div class="val">{summary.get('caselaw_files',0)}</div><div class="lbl">Case law → RAG</div></div>
  <div class="stat"><div class="val">{summary.get('private_files',0)}</div><div class="lbl">Private → finetune</div></div>
  <div class="stat"><div class="val">{summary.get('uncertain_files',0)}</div><div class="lbl">Uncertain → finetune+flagged</div></div>
  <div class="stat"><div class="val">{summary.get('published_files',0)}</div><div class="lbl">Published → RAG+finetune</div></div>
</div>

<h2>Per-File Results</h2>
<table>
  <tr><th>File</th><th>Type</th><th>Tokens</th><th>PII flags</th><th>Chunks</th><th>OCR</th></tr>
  {file_rows}
</table>

{'<h2>PII Review — ' + str(len(flags)) + ' flagged entities</h2>' if flags else ''}
{'<div style="background:#fefce8;border:1px solid #fde047;border-radius:6px;padding:12px 16px;margin-bottom:16px;font-size:12px;color:#713f12"><strong>⚠ Audit trail note:</strong> Clicking buttons in this page does not record anything. Your decisions are only saved when you click <strong>Download Decisions CSV</strong> and then run <code>python3.12 apply_decisions.py --decisions decisions.csv --output /path/to/output --reviewer "Your Name"</code>. That command archives the decisions file, creates an audit entry in provenance.json, and (if needed) patches the dataset.</div>' if flags else ''}\n{'<p style="color:#555;font-size:12px">All were <strong>redacted</strong> with Faker replacements. Mark any that were <strong>wrongly</strong> detected (not actually PII) using the buttons below, then download and run apply_decisions.py to record your review.</p>' if flags else ''}

{'''<h2>Batch Actions by Entity Type</h2>
<table>
  <tr><th>Entity type</th><th>Count</th><th>% of flags</th><th>Batch action</th></tr>
''' + type_rows + '</table>' if flags else ''}

{'''<h2>Batch Actions by File</h2>
<table>
  <tr><th>File</th><th>Flags</th><th>Top entity types</th><th>Batch action</th></tr>
''' + file_summary_rows + '</table>' if flags else ''}

{'<h2>All Flagged Entities</h2>' if flags else ''}
{'<div id="filter-bar" class="no-print"><input type="text" id="search" placeholder="Filter by file or text..." oninput="applyFilter()"><select id="type-filter" onchange="applyFilter()"><option value="">All entity types</option>' + ''.join(f'<option>{t}</option>' for t in sorted(by_type.keys())) + '</select><button class="batch-btn" onclick="batchFiltered(\'approve\')">Approve filtered</button><button class="batch-btn restore" onclick="batchFiltered(\'restore\')">Restore filtered</button></div>' if flags else ''}

<table id="flags-table" {'style="display:none"' if not flags else ''}>
  <tr><th>#</th><th>File</th><th>Type</th><th>Detected text</th><th>Conf.</th><th>Context</th><th class="no-print">Decision</th></tr>
</table>

<div class="result-banner" id="result-banner"></div>
<div id="toolbar" class="no-print">
  <div class="tl-stat">Keep redacted: <span id="cnt-approve">0</span></div>
  <div class="tl-stat">Restore: <span id="cnt-restore">0</span></div>
  <div class="tl-stat">Unreviewed: <span id="cnt-pending">0</span></div>
  <input id="reviewer-name" type="text" placeholder="Your name (required for audit trail)">
  <button id="apply-btn" onclick="applyDecisions()">✓ Apply Decisions</button>
  <button id="dl-btn" onclick="downloadDecisions()">⬇ Download CSV</button>
</div>

<script>
const FLAGS = {flags_json};
const FILE_INDEX = {file_index_json};
const decisions = {{}};  // flag_id -> 'approve' | 'restore'

function flash(btn, count, decision) {{
  const orig = btn.textContent;
  const origClass = btn.className;
  btn.textContent = decision === 'approve' ? `✓ ${{count}} marked correct` : `✗ ${{count}} marked wrong`;
  btn.style.fontWeight = '700';
  btn.style.opacity = '1';
  setTimeout(() => {{ btn.textContent = orig; btn.className = origClass; btn.style.fontWeight = ''; btn.style.opacity = ''; }}, 2000);
}}

function toggle(id) {{
  decisions[id] = decisions[id] === 'restore' ? 'approve' : 'restore';
  renderRow(id);
  updateCounts();
}}

function renderRow(id) {{
  const btn = document.getElementById('dec-' + id);
  if (!btn) return;
  const d = decisions[id] || 'approve';
  btn.textContent = d === 'approve' ? '✓ Keep redacted' : '✗ Wrong — restore';
  btn.className = 'dec-btn dec-' + d;
  const row = document.getElementById('row-' + id);
  if (row) row.style.background = d === 'restore' ? '#fff1f2' : '';
}}

function batchTypeBtn(btn) {{
  const type = btn.dataset.btype;
  const decision = btn.dataset.dec;
  const affected = FLAGS.filter(f => f.entity_type === type);
  affected.forEach(f => {{ decisions[f.id] = decision; renderRow(f.id); }});
  updateCounts();
  flash(btn, affected.length, decision);
}}

function batchFileBtn(btn) {{
  const file = FILE_INDEX[parseInt(btn.dataset.bfileIdx)];
  const decision = btn.dataset.dec;
  const affected = FLAGS.filter(f => f.file === file);
  affected.forEach(f => {{ decisions[f.id] = decision; renderRow(f.id); }});
  updateCounts();
  flash(btn, affected.length, decision);
}}

function batchFiltered(decision) {{
  const ids = visibleIds();
  ids.forEach(id => {{ decisions[id] = decision; renderRow(id); }});
  updateCounts();
}}

function visibleIds() {{
  const rows = document.querySelectorAll('#flags-table tr[id^="row-"]');
  return Array.from(rows).filter(r => r.style.display !== 'none').map(r => parseInt(r.id.replace('row-','')));
}}

function updateCounts() {{
  const vals = Object.values(decisions);
  document.getElementById('cnt-approve').textContent = vals.filter(v=>v==='approve').length;
  document.getElementById('cnt-restore').textContent = vals.filter(v=>v==='restore').length;
  document.getElementById('cnt-pending').textContent = FLAGS.length - vals.length;
}}

function applyFilter() {{
  const search = document.getElementById('search').value.toLowerCase();
  const typeF = document.getElementById('type-filter').value;
  document.querySelectorAll('#flags-table tr[id^="row-"]').forEach(row => {{
    const f = FLAGS[parseInt(row.id.replace('row-',''))];
    const match = (!search || f.file.toLowerCase().includes(search) || f.original_text.toLowerCase().includes(search))
                  && (!typeF || f.entity_type === typeF);
    row.style.display = match ? '' : 'none';
  }});
}}

function downloadDecisions() {{
  const rows = ['flag_id,file,entity_type,original_text,confidence,decision'];
  FLAGS.forEach(f => {{
    const d = decisions[f.id] || 'approve';
    const esc = s => '"' + String(s).replace(/"/g,'""') + '"';
    rows.push([f.id, esc(f.file), f.entity_type, esc(f.original_text), f.confidence, d].join(','));
  }});
  const blob = new Blob([rows.join('\\n')], {{type:'text/csv'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'decisions.csv';
  a.click();
}}

// Build flag rows on load
window.addEventListener('DOMContentLoaded', () => {{
  if (!FLAGS.length) return;
  const tbody = document.querySelector('#flags-table');
  FLAGS.forEach(f => {{
    const tr = document.createElement('tr');
    tr.id = 'row-' + f.id;
    tr.innerHTML = `
      <td style="color:#aaa">${{f.id+1}}</td>
      <td style="font-size:11px">${{esc(f.file)}}</td>
      <td><span class="tag tag-pii">${{f.entity_type}}</span></td>
      <td><strong>${{esc(f.original_text)}}</strong></td>
      <td class="conf">${{f.confidence.toFixed(2)}}</td>
      <td class="ctx">...${{esc(f.context)}}...</td>
      <td class="no-print"><button id="dec-${{f.id}}" class="dec-btn dec-approve" onclick="toggle(${{f.id}})">✓ Keep redacted</button></td>
    `;
    tbody.appendChild(tr);
  }});
  document.getElementById('flags-table').style.display = '';
  updateCounts();
}});

function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

// Detect whether we're being served by review_server.py
fetch('/status').then(r => r.json()).then(s => {{
  if (s.server) {{
    document.getElementById('apply-btn').style.display = '';
    document.getElementById('dl-btn').style.marginLeft = '0';
  }}
}}).catch(() => {{}});

async function applyDecisions() {{
  const reviewer = document.getElementById('reviewer-name').value.trim();
  if (!reviewer) {{
    alert('Please enter your name in the reviewer field before applying.');
    document.getElementById('reviewer-name').focus();
    return;
  }}

  const unreviewed = FLAGS.length - Object.keys(decisions).length;
  const restoreCount = Object.values(decisions).filter(v => v === 'restore').length;
  const msg = unreviewed > 0
    ? `${{unreviewed}} flags not explicitly reviewed — they will be treated as "keep redacted".\n\n${{restoreCount}} will be restored.\n\nProceed?`
    : `${{restoreCount}} entities will be restored to original text. Proceed?`;

  if (!confirm(msg)) return;

  const all = FLAGS.map(f => ({{
    flag_id: f.id,
    file: f.file,
    entity_type: f.entity_type,
    original_text: f.original_text,
    confidence: f.confidence,
    decision: decisions[f.id] || 'approve',
  }}));

  const btn = document.getElementById('apply-btn');
  btn.textContent = 'Applying…';
  btn.disabled = true;

  try {{
    const resp = await fetch('/apply', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{decisions: all, reviewer}}),
    }});
    const result = await resp.json();
    if (result.ok) {{
      const banner = document.getElementById('result-banner');
      banner.textContent = `✓ Review complete — ${{result.approved}} kept redacted, ${{result.restored}} restored, audit record saved as ${{result.decisions_file}}`;
      banner.style.display = 'block';
      btn.textContent = '✓ Applied';
      btn.style.background = '#15803d';
      setTimeout(() => window.location.reload(), 3000);
    }} else {{
      alert('Server error: ' + result.error);
      btn.textContent = '✓ Apply Decisions';
      btn.disabled = false;
    }}
  }} catch(e) {{
    alert('Could not reach server: ' + e.message);
    btn.textContent = '✓ Apply Decisions';
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""

    out_path.write_text(html, encoding='utf-8')
