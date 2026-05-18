#!/usr/bin/env python3
"""
Re-identification risk assessment for the legal-doc-processor pipeline.

Sends sampled records from the finetune dataset to Claude (adversarial framing)
and assesses whether de-identified documents can still be linked to real individuals
via quasi-identifiers and public records.

Usage:
    python3.12 re_id_risk.py --output /path/to/output [--samples 20] [--quiet]

Output:
    output/re_id_risk_report.json   — full per-record assessments + summary
    Appends a risk section to output/summary.html if it exists
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

_SYSTEM_PROMPT = """\
You are a privacy expert and adversarial re-identification researcher. Your sole job is \
to determine whether a de-identified legal document can be linked back to a specific \
real individual, given access to public records and open sources.

Assume the adversary is motivated and has access to:
- Federal and state court dockets (PACER, CourtListener)
- Newspaper archives, local news, and social media
- Medical provider and hospital directories
- Property records, voter rolls, professional licensing databases
- Google and general web search

For the document you receive, identify "quasi-identifiers": combinations of retained \
facts (medical conditions, facility names, dates, specialties, incident type, geography, \
procedural posture, attorney names, case numbers, monetary amounts) that together narrow \
the population down to a single individual even without an explicit name.

Be specific, adversarial, and honest. If the document is genuinely low-risk, say so.

Respond ONLY with valid JSON matching this exact schema — no prose, no code fences:
{
  "risk_level": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "risk_summary": "One-sentence overall verdict",
  "quasi_identifiers": [
    {"type": "string", "value": "exact text still present", "concern": "why this narrows identity"}
  ],
  "reconstruction_path": "Step-by-step: how would an adversary actually find this person?",
  "additional_redactions": ["specific phrases or patterns that should be removed"],
  "recommendations": ["actionable steps to reduce risk"],
  "confidence": 0.0
}"""

# Characters to sample per segment (~2 000 tokens each at 4 chars/token)
_SEG_CHARS = 8_000
_MAX_SEGS = 3


def _sample_text(text: str) -> str:
    total = _SEG_CHARS * _MAX_SEGS
    if len(text) <= total:
        return text
    first = text[:_SEG_CHARS]
    mid_start = (len(text) - _SEG_CHARS) // 2
    middle = text[mid_start: mid_start + _SEG_CHARS]
    last = text[-_SEG_CHARS:]
    return (
        f"{first}\n\n[… middle of document omitted …]\n\n"
        f"{middle}\n\n[… end of document follows …]\n\n{last}"
    )


def _assess_record(client: anthropic.Anthropic, record: dict) -> dict:
    meta = record.get('metadata', {})
    text = record.get('text', '')
    source = meta.get('source', 'unknown')
    doc_type = meta.get('doc_type', 'unknown')
    review_flags = meta.get('review_flags', 0)
    token_count = meta.get('token_count', 0)

    sampled = _sample_text(text)
    user_msg = (
        f"Analyze this de-identified legal document for re-identification risk.\n\n"
        f"Document type: {doc_type}\n"
        f"PII review flags during Presidio processing: {review_flags}\n"
        f"Approximate token count: {token_count:,}\n\n"
        f"<document>\n{sampled}\n</document>"
    )

    with client.messages.stream(
        model='claude-opus-4-7',
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        final = stream.get_final_message()

    response_text = ''
    for block in final.content:
        if block.type == 'text':
            response_text = block.text
            break

    try:
        cleaned = response_text.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('\n', 1)[1].rsplit('```', 1)[0]
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {
            'risk_level': 'PARSE_ERROR',
            'risk_summary': 'Claude returned non-JSON response.',
            'raw_response': response_text[:800],
        }

    result['_source'] = source
    result['_doc_type'] = doc_type
    result['_review_flags'] = review_flags
    result['_token_count'] = token_count
    result['_input_tokens'] = final.usage.input_tokens
    result['_output_tokens'] = final.usage.output_tokens
    return result


_RISK_ORDER = {'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}


def run_assessment(output_dir: Path, max_samples: int = 20, quiet: bool = False) -> dict:
    dataset_path = output_dir / 'finetune' / 'dataset.jsonl'
    if not dataset_path.exists():
        print(f"No finetune dataset at {dataset_path}", file=sys.stderr)
        return {}

    records = []
    with open(dataset_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Prioritise highest PII-flag records first
    records.sort(key=lambda r: r.get('metadata', {}).get('review_flags', 0), reverse=True)
    samples = records[:max_samples]

    if not quiet:
        print(f"\nRe-identification Risk Assessment")
        print(f"  Dataset:  {dataset_path}")
        print(f"  Records:  {len(records)} total, assessing {len(samples)}")
        print(f"  Model:    claude-opus-4-7\n")

    client = anthropic.Anthropic()
    assessments = []

    for i, record in enumerate(samples, 1):
        meta = record.get('metadata', {})
        source = meta.get('source', f'record_{i}')
        flags = meta.get('review_flags', 0)
        if not quiet:
            print(f"  [{i:2d}/{len(samples)}] {source}  ({flags} PII flags)...")
        try:
            result = _assess_record(client, record)
            assessments.append(result)
            if not quiet:
                level = result.get('risk_level', '?')
                print(f"         → {level}")
        except Exception as exc:
            if not quiet:
                print(f"         → ERROR: {exc}")
            assessments.append({
                '_source': source,
                '_doc_type': meta.get('doc_type', ''),
                '_review_flags': flags,
                'risk_level': 'ERROR',
                'risk_summary': str(exc),
            })

    risk_dist: dict[str, int] = {}
    for a in assessments:
        level = a.get('risk_level', 'UNKNOWN')
        risk_dist[level] = risk_dist.get(level, 0) + 1

    highest = max(
        (a.get('risk_level', 'LOW') for a in assessments),
        key=lambda l: _RISK_ORDER.get(l, 0),
        default='LOW',
    )

    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'total_records_in_dataset': len(records),
        'records_assessed': len(assessments),
        'risk_distribution': risk_dist,
        'highest_risk_level': highest,
        'assessments': assessments,
    }

    report_path = output_dir / 're_id_risk_report.json'
    report_path.write_text(json.dumps(report, indent=2))
    if not quiet:
        print(f"\n  Report saved → {report_path}")

    _append_risk_section(output_dir / 'summary.html', report)

    return report


# ---------------------------------------------------------------------------
# HTML section appended to summary.html
# ---------------------------------------------------------------------------

def _risk_colour(level: str) -> str:
    return {
        'CRITICAL': '#fca5a5',
        'HIGH':     '#fde68a',
        'MEDIUM':   '#bfdbfe',
        'LOW':      '#bbf7d0',
    }.get(level, '#e5e7eb')


def _risk_text_colour(level: str) -> str:
    return {
        'CRITICAL': '#7f1d1d',
        'HIGH':     '#78350f',
        'MEDIUM':   '#1e3a5f',
        'LOW':      '#14532d',
    }.get(level, '#374151')


def _append_risk_section(html_path: Path, report: dict) -> None:
    if not html_path.exists():
        return

    assessments = report.get('assessments', [])
    highest = report.get('highest_risk_level', 'LOW')
    dist = report.get('risk_distribution', {})
    generated = report.get('generated_at', '')[:19].replace('T', ' ')

    dist_pills = ' '.join(
        f'<span style="display:inline-block;padding:2px 10px;border-radius:3px;font-size:12px;font-weight:700;'
        f'background:{_risk_colour(lvl)};color:{_risk_text_colour(lvl)}">{lvl}: {n}</span>'
        for lvl, n in sorted(dist.items(), key=lambda x: -_RISK_ORDER.get(x[0], 0))
    )

    rows = ''
    for a in assessments:
        level = a.get('risk_level', '?')
        bg = _risk_colour(level)
        tc = _risk_text_colour(level)
        qi = a.get('quasi_identifiers', [])
        qi_str = '; '.join(
            f"{q.get('type','')}: <em>{_hesc(str(q.get('value',''))[:80])}</em>"
            for q in qi[:4]
        ) or '—'
        recs = a.get('recommendations', [])
        rec_str = (' '.join(
            f'<li>{_hesc(r)}</li>' for r in recs[:3]
        ))
        rows += (
            f'<tr>'
            f'<td style="font-size:11px">{_hesc(a.get("_source",""))}</td>'
            f'<td><span style="display:inline-block;padding:1px 7px;border-radius:3px;font-size:11px;font-weight:700;'
            f'background:{bg};color:{tc}">{_hesc(level)}</span></td>'
            f'<td style="font-size:11px">{_hesc(a.get("risk_summary",""))}</td>'
            f'<td style="font-size:11px">{qi_str}</td>'
            f'<td style="font-size:11px"><ul style="margin:0;padding-left:16px">{rec_str}</ul></td>'
            f'</tr>\n'
        )

    section = f"""
<!-- re-id-risk-section -->
<h2 style="margin-top:36px">Re-Identification Risk Assessment</h2>
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:12px 16px;margin-bottom:16px;font-size:12px;color:#475569">
  Assessed by <strong>claude-opus-4-7</strong> (adversarial framing) &nbsp;·&nbsp; {generated} UTC &nbsp;·&nbsp;
  {report.get('records_assessed',0)} of {report.get('total_records_in_dataset',0)} records sampled (highest PII-flag count first)
  &nbsp;·&nbsp; Overall highest risk: <strong style="color:{_risk_text_colour(highest)}">{_hesc(highest)}</strong>
</div>
<div style="margin-bottom:12px">{dist_pills}</div>
<table>
  <tr>
    <th>Record</th><th>Risk</th><th>Summary</th><th>Quasi-identifiers</th><th>Recommendations</th>
  </tr>
  {rows}
</table>
<!-- /re-id-risk-section -->
"""

    html = html_path.read_text(encoding='utf-8')
    # Remove stale section if present, then insert before </body>
    if '<!-- re-id-risk-section -->' in html:
        start = html.index('<!-- re-id-risk-section -->')
        end = html.index('<!-- /re-id-risk-section -->') + len('<!-- /re-id-risk-section -->')
        html = html[:start] + html[end:]

    html = html.replace('</body>', section + '\n</body>', 1)
    html_path.write_text(html, encoding='utf-8')


def _hesc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Adversarial re-identification risk assessment via Claude API.'
    )
    parser.add_argument('--output', required=True, type=Path,
                        help='Pipeline output directory (contains finetune/dataset.jsonl)')
    parser.add_argument('--samples', type=int, default=20,
                        help='Max records to assess (default: 20, highest-PII-flag first)')
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress progress output')
    args = parser.parse_args()

    report = run_assessment(args.output, max_samples=args.samples, quiet=args.quiet)
    if not report:
        sys.exit(1)

    highest = report.get('highest_risk_level', 'UNKNOWN')
    dist = report.get('risk_distribution', {})
    print(f"\nSummary: highest risk = {highest}")
    for level in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
        if level in dist:
            print(f"  {level}: {dist[level]} record(s)")
    if 'ERROR' in dist:
        print(f"  ERROR: {dist['ERROR']} record(s) (check report for details)")


if __name__ == '__main__':
    main()
