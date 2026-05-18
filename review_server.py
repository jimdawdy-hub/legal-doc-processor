#!/usr/bin/env python3
"""
Local review server for the legal-doc-processor pipeline.
Serves summary.html and applies decisions without manual CSV handling.

Usage:
    python3.12 review_server.py --output /path/to/output [--port 8765]

Workflow:
    1. Run this script — browser opens automatically
    2. Click through decisions in the browser
    3. Enter your name in the reviewer field
    4. Click "Apply Decisions" — server patches the dataset and writes the audit record
    5. Ctrl+C to stop the server when done
"""

import csv
import json
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apply_decisions import build_exclusion_map, reprocess_file
from reporter import generate_reports


class ReviewHandler(BaseHTTPRequestHandler):
    output_dir: Path = None

    def do_GET(self):
        if self.path in ('/', '/summary.html'):
            self._serve_file(self.output_dir / 'summary.html', 'text/html; charset=utf-8')
        elif self.path == '/status':
            self._json({'server': True, 'output_dir': str(self.output_dir)})
        elif self.path == '/provenance':
            self._serve_file(self.output_dir / 'provenance.json', 'application/json')
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/apply':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            try:
                result = self._apply(body)
                self._json(result)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json({'error': str(e)}, 500)
        else:
            self.send_error(404)

    def _apply(self, body):
        decisions = body.get('decisions', [])
        reviewer = body.get('reviewer', '').strip()

        restores = [d for d in decisions if d.get('decision') == 'restore']
        approves  = [d for d in decisions if d.get('decision') != 'restore']

        timestamp = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')

        # Load provenance first so we know where the source case folder is
        provenance_path = self.output_dir / 'provenance.json'
        provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else {}

        # Archive decisions CSV to the source case folder (not the derived dataset folder)
        source_dir = provenance.get('source_dir')
        if source_dir and Path(source_dir).exists():
            audit_dir = Path(source_dir) / 'audit'
        else:
            audit_dir = self.output_dir / 'review'
        audit_dir.mkdir(parents=True, exist_ok=True)
        archived = audit_dir / f'decisions-{stamp}.csv'
        fieldnames = ['flag_id', 'file', 'entity_type', 'original_text', 'confidence', 'decision']
        with open(archived, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for d in decisions:
                writer.writerow(d)

        print(f"\n  Reviewer: {reviewer or '(unnamed)'}")
        print(f"  Decisions: {len(approves)} keep-redacted, {len(restores)} restore")
        print(f"  Archived to: {archived}")

        audit_entry = {
            'reviewed_at': timestamp,
            'reviewer': reviewer,
            'decisions_file': str(archived),
            'total_flags_reviewed': len(decisions),
            'approved_keep_redacted': len(approves),
            'restored_original': len(restores),
        }
        provenance.setdefault('human_reviews', []).append(audit_entry)

        files_changed = 0
        if restores:
            exclusion_map = build_exclusion_map(restores)
            print(f"  Re-processing {len(exclusion_map)} file(s)...")
            updated = []
            for rec in provenance.get('files', []):
                fname = rec['original_filename']
                if fname in exclusion_map:
                    src = Path(rec.get('source_path', ''))
                    if src.exists():
                        rec = reprocess_file(src, exclusion_map[fname], self.output_dir, rec)
                        files_changed += 1
                    else:
                        print(f"  WARNING: source not found: {src}")
                updated.append(rec)
            provenance['files'] = updated
            provenance['summary']['pii_review_flags'] = sum(
                r.get('processing', {}).get('review_flags', 0)
                for r in updated if not r.get('skipped')
            )

        provenance_path.write_text(json.dumps(provenance, indent=2))
        print("  Regenerating reports...")
        generate_reports(self.output_dir)
        print("  Done.\n")

        return {
            'ok': True,
            'reviewed_at': timestamp,
            'reviewer': reviewer,
            'approved': len(approves),
            'restored': len(restores),
            'files_reprocessed': files_changed,
            'decisions_file': archived.name,
        }

    def _serve_file(self, path, content_type):
        try:
            content = Path(path).read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Suppress per-request noise except errors
        if args and str(args[1]) not in ('200', '204'):
            print(f"  [{self.address_string()}] {fmt % args}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Serve the pipeline review UI locally.')
    parser.add_argument('--output', required=True, type=Path,
                        help='Pipeline output directory containing summary.html')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    html_path = args.output / 'summary.html'
    if not html_path.exists():
        print(f"Error: {html_path} not found. Run the pipeline first.", file=sys.stderr)
        sys.exit(1)

    ReviewHandler.output_dir = args.output.resolve()
    server = HTTPServer(('localhost', args.port), ReviewHandler)
    url = f'http://localhost:{args.port}/'

    print(f"Legal Doc Processor — Review Server")
    print(f"  URL:    {url}")
    print(f"  Output: {args.output.resolve()}")
    print(f"  Press Ctrl+C to stop.\n")

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == '__main__':
    main()
