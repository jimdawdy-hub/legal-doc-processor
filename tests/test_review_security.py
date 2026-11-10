import http.client
import json
import threading
from http.server import HTTPServer
from unittest.mock import patch

import pytest
from presidio_analyzer import RecognizerResult
from review_server import ReviewHandler
from apply_decisions import apply_review
from pipeline import process_directory
from reporter import generate_reports
from second_pass import _extract_targets, _compile_patterns, _apply_patterns, _patch_jsonl, run_second_pass
from utils import sha256_file


@pytest.fixture
def review_http(tmp_path):
    (tmp_path / 'summary.html').write_text('<html>local private review</html>')
    server = HTTPServer(('127.0.0.1', 0), ReviewHandler)
    server.output_dir = tmp_path
    server.review_token = 'synthetic-test-session'
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    with patch('review_server.apply_review', return_value={'ok': True}) as apply:
        yield server, apply
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def request(server, method, path, body=None, *, authenticated=False, headers=None):
    h = {'Host': f'127.0.0.1:{server.server_port}', **(headers or {})}
    if authenticated:
        h.update(Cookie='review_session=synthetic-test-session')
        h.setdefault('X-Review-Token', 'synthetic-test-session')
    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse(); result = r.status, dict(r.getheaders()), r.read(); conn.close()
    return result


def test_private_report_requires_session(review_http):
    server, apply = review_http
    assert request(server, 'GET', '/')[0] == 401
    status, headers, _ = request(server, 'GET', '/?token=synthetic-test-session')
    assert status == 303
    assert 'HttpOnly' in headers['Set-Cookie']
    assert 'SameSite=Strict' in headers['Set-Cookie']
    assert request(server, 'GET', '/', authenticated=True)[0] == 200
    assert 'Access-Control-Allow-Origin' not in headers
    apply.assert_not_called()


@pytest.mark.parametrize('headers', [{'Host': 'attacker.invalid'}, {'Origin': 'https://attacker.invalid'}])
def test_rebinding_and_foreign_origin_cannot_read_or_write(review_http, headers):
    server, apply = review_http
    assert request(server, 'GET', '/status', authenticated=True, headers=headers)[0] == 403
    assert request(server, 'POST', '/apply', b'{}', authenticated=True, headers=headers)[0] == 403
    apply.assert_not_called()


def test_mutation_needs_token_and_well_formed_bounded_json(review_http):
    server, apply = review_http
    assert request(server, 'POST', '/apply', b'{}')[0] == 403
    assert request(server, 'POST', '/apply', b'{}', authenticated=True,
                   headers={'X-Review-Token': 'wrong', 'Content-Type': 'application/json'})[0] == 403
    assert request(server, 'POST', '/apply', b'bad json', authenticated=True,
                   headers={'Content-Type': 'application/json'})[0] == 400
    assert request(server, 'POST', '/apply', b'', authenticated=True,
                   headers={'Content-Type': 'application/json', 'Content-Length': str(20 * 1024 * 1024)})[0] == 413
    apply.assert_not_called()
    body = json.dumps({'decisions': [], 'reviewer': 'Synthetic reviewer'})
    assert request(server, 'POST', '/apply', body, authenticated=True,
                   headers={'Content-Type': 'application/json'})[0] == 200
    apply.assert_called_once()


def test_html_escapes_script_closing_sequences_and_entity_types(tmp_path):
    review = tmp_path / 'review'; review.mkdir()
    payload = '</script><script>alert("synthetic")</script>'
    flag = {'file': payload, 'entity_type': '<img src=x onerror=alert(1)>',
            'confidence': 0.7, 'original_text': payload, 'context': payload}
    (review / 'review_log.jsonl').write_text(json.dumps(flag) + '\n')
    generate_reports(tmp_path)
    html = (tmp_path / 'summary.html').read_text()
    assert payload not in html
    assert '<img src=x onerror=alert(1)>' not in html
    assert html.count('<script>') == 1
    assert '\\u003c/script\\u003e' in html


def test_date_target_is_not_split_into_broad_numeric_fragments():
    report = {'assessments': [{'quasi_identifiers': [{'type': 'date_of_birth', 'value': '03/14/1979'}]}]}
    targets = _extract_targets(report)
    assert targets == [('03/14/1979', '[DATE]')]
    text, count = _apply_patterns('On 03/14/1979; section 03 remains.', _compile_patterns(targets))
    assert text == 'On [DATE]; section 03 remains.'
    assert count == 1


def test_literal_replacement_is_not_interpreted_as_regex_backreference():
    text, _ = _apply_patterns('Synthetic name', _compile_patterns([('Synthetic name', r'[ID\1]')]))
    assert text == r'[ID\1]'


def test_second_pass_updates_token_metadata_and_refuses_stale_assessment(tmp_path):
    dataset = tmp_path / 'finetune/dataset.jsonl'; dataset.parent.mkdir()
    dataset.write_text(json.dumps({'text': 'Synthetic Example Name', 'metadata': {'token_count': 999}}) + '\n')
    report = {'dataset_sha256': 'incorrect', 'assessments': []}
    (tmp_path / 're_id_risk_report.json').write_text(json.dumps(report))
    with pytest.raises(ValueError, match='Dataset changed'):
        run_second_pass(tmp_path, quiet=True)
    _patch_jsonl(dataset, _compile_patterns([('Synthetic Example Name', '[PERSON]')]), False, False)
    assert json.loads(dataset.read_text())['metadata']['token_count'] != 999


def test_restore_targets_stable_id_and_keeps_other_pii_redacted_in_both_exports(tmp_path):
    source = tmp_path / 'input'; source.mkdir()
    text = 'Private Name, SSN 123-45-6789\n© 2024 Example Hospital'
    (source / 'example.txt').write_text(text)
    class Analyzer:
        def analyze(self, text, **kwargs):
            start = text.index('123-45-6789')
            return [RecognizerResult('PERSON', 0, len('Private Name'), 0.7),
                    RecognizerResult('US_SSN', start, start + 11, 0.9)]
    out = tmp_path / 'output'
    with patch('pii._get_engines', return_value=(Analyzer(), None)):
        process_directory(source, out)
        flag = json.loads((out / 'review/review_log.jsonl').read_text())
        decision = {**flag, 'flag_id': 0, 'decision': 'restore'}
        result = apply_review(out, [decision], 'Synthetic reviewer')
    assert result['restored'] == 1
    for path in [out / 'finetune/dataset.jsonl', *list((out / 'rag').glob('*.jsonl'))]:
        data = path.read_text()
        assert 'Private Name' in data
        assert '123-45-6789' not in data
    data = json.loads((out / 'provenance.json').read_text())
    assert data['human_reviews'][0]['reviewer'] == 'Synthetic reviewer'


def test_restore_cannot_undo_second_pass_or_forge_a_flag(tmp_path):
    (tmp_path / 'review').mkdir()
    flag = {'file': 'anon_synthetic', 'entity_type': 'PERSON', 'original_text': 'Synthetic Name'}
    (tmp_path / 'review/review_log.jsonl').write_text(json.dumps(flag))
    (tmp_path / 'provenance.json').write_text(json.dumps({'files': [], 'second_pass_redactions': [{}]}))
    with pytest.raises(ValueError, match='undo second-pass'):
        apply_review(tmp_path, [{**flag, 'flag_id': 0, 'decision': 'restore'}], 'Reviewer')
    with pytest.raises(ValueError, match='no longer matches'):
        apply_review(tmp_path, [{**flag, 'original_text': 'different', 'flag_id': 0, 'decision': 'restore'}], 'Reviewer')


def test_failed_multi_file_restore_keeps_original_exports_unchanged(tmp_path):
    out = tmp_path / 'out'; (out / 'review').mkdir(parents=True); (out / 'finetune').mkdir()
    files, flags = [], []
    for i in range(2):
        source = tmp_path / f'synthetic-{i}.txt'; source.write_text('original synthetic')
        files.append({'source_id': f'anon_{i}', 'source_path': str(source), 'sha256': sha256_file(source)})
        flags.append({'file': f'anon_{i}', 'entity_type': 'PERSON', 'original_text': 'Synthetic Name'})
    (out / 'review/review_log.jsonl').write_text(''.join(json.dumps(f) + '\n' for f in flags))
    dataset = out / 'finetune/dataset.jsonl'; dataset.write_text('original redacted exports')
    (out / 'provenance.json').write_text(json.dumps({'files': files}))
    decisions = [{**f, 'flag_id': i, 'decision': 'restore'} for i, f in enumerate(flags)]
    called = []
    def fail_on_second(source, exclusions, stage, rec):
        called.append(source)
        (stage / 'finetune/dataset.jsonl').write_text('staged restored text')
        if len(called) == 2:
            raise RuntimeError('simulated NLP failure')
        return rec
    with patch('apply_decisions.reprocess_file', side_effect=fail_on_second):
        with pytest.raises(RuntimeError, match='simulated'):
            apply_review(out, decisions, 'Reviewer')
    assert dataset.read_text() == 'original redacted exports'
    assert not list(tmp_path.glob('.review-stage-*'))
    assert not list((out / 'review').glob('decisions-*.csv'))


def test_spreadsheet_export_escapes_formulas_and_decisions_round_trip(tmp_path):
    import csv
    from apply_decisions import load_decisions, validate_decisions
    review = tmp_path / 'review'; review.mkdir()
    flag = {'file': 'anon_synthetic', 'entity_type': 'PERSON', 'original_text': '=HYPERLINK("test")',
            'context': '+synthetic formula', 'confidence': 0.7}
    (review / 'review_log.jsonl').write_text(json.dumps(flag))
    generate_reports(tmp_path)
    path = review / 'review_log.csv'
    row = list(csv.DictReader(path.open()))[0]
    assert row['original_text'].startswith("'=")
    assert row['context'].startswith("'+")
    restores, approves = load_decisions(path)
    canonical = validate_decisions(tmp_path, restores + approves)
    assert canonical[0]['original_text'] == flag['original_text']


def test_manual_blocklist_does_not_require_an_external_ai_call(tmp_path):
    dataset = tmp_path / 'finetune/dataset.jsonl'; dataset.parent.mkdir()
    dataset.write_text(json.dumps({'text': 'Synthetic Name', 'metadata': {'token_count': 9}}) + '\n')
    result = run_second_pass(tmp_path, extra_blocklist=[('Synthetic Name', '[PERSON]')], quiet=True)
    assert result['total_substitutions'] == 1
    assert json.loads(dataset.read_text())['text'] == '[PERSON]'
