#!/usr/bin/env python3
"""Private, loopback-only browser review of local legal-document outputs."""
import argparse
import hmac
import json
import secrets
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from apply_decisions import apply_review
from utils import write_json

MAX_BODY = 10 * 1024 * 1024


class ReviewHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    @property
    def output_dir(self):
        return self.server.output_dir

    def _allowed_origin(self):
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        return (self.headers.get('Host') in hosts
                and (origin is None or origin in {'http://' + host for host in hosts}))

    def _authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookie['review_session'].value if 'review_session' in cookie else ''
            return hmac.compare_digest(value, self.server.review_token)
        except Exception:
            return False

    def do_GET(self):
        if not self._allowed_origin():
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        token = parse_qs(parsed.query).get('token', [''])[0]
        if parsed.path == '/' and token and hmac.compare_digest(token, self.server.review_token):
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'review_session=' + token + '; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self._authenticated():
            self.send_error(401)
            return
        if self.path in ('/', '/summary.html'):
            self._serve_file(self.output_dir / 'summary.html', 'text/html; charset=utf-8')
        elif self.path == '/status':
            self._json({'server': True, 'review_token': self.server.review_token})
        elif self.path == '/provenance':
            self._serve_file(self.output_dir / 'provenance.json', 'application/json')
        else:
            self.send_error(404)

    def do_POST(self):
        self.close_connection = True
        if not self._allowed_origin() or not self._authenticated():
            self.send_error(403)
            return
        if self.path != '/apply':
            self.send_error(404)
            return
        if not hmac.compare_digest(self.headers.get('X-Review-Token', ''), self.server.review_token):
            self.send_error(403)
            return
        if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/json':
            self.send_error(400)
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= length <= MAX_BODY:
                self.send_error(413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete request')
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('Expected an object')
            result = apply_review(self.output_dir, body.get('decisions'), body.get('reviewer'))
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            self._json({'error': 'Invalid or stale review decisions; refresh and check the local batch.'}, 400)
            return
        except (OSError, RuntimeError):
            self._json({'error': 'Local review failed; inspect the batch before retrying.'}, 500)
            return
        self._json(result)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        super().end_headers()

    def _serve_file(self, path, content_type):
        try:
            content = path.read_bytes()
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
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, *args):
        pass  # Never log bootstrap query strings or document identifiers.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    if not (args.output / 'summary.html').is_file():
        parser.error('Run the local pipeline first; summary.html is missing')
    server = HTTPServer(('127.0.0.1', args.port), ReviewHandler)
    server.output_dir = args.output.resolve()
    server.review_token = secrets.token_urlsafe(32)
    url = f'http://127.0.0.1:{server.server_port}/?token={server.review_token}'
    session_path = server.output_dir / 'review' / f'.browser-session-{server.server_port}.json'
    write_json(session_path, {'url': url})
    print(f'Local review server on 127.0.0.1:{server.server_port}')
    print(f'Private browser link saved to {session_path} (keep this file local).')
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session_path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
