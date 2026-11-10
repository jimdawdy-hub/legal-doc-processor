import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from ai_client import AIClient
from re_id_risk import _assess_record, _summarize, retry_errors
from utils import sha256_file


def policy():
    return {'endpoint': 'https://approved.example/v1/chat/completions', 'models': ['approved-model'],
            'plan': 'business', 'training_on_user_data': False,
            'zdr_required': True, 'zdr_enabled': True,
            'policy_reference': 'Synthetic test contract and tenant settings'}


def make_client(**kwargs):
    return AIClient(policy()['endpoint'], 'approved-model', policy=kwargs.pop('policy', policy()),
                    approved_ai_use=kwargs.pop('approved_ai_use', True), **kwargs)


@pytest.mark.parametrize('field,value', [
    ('training_on_user_data', True), ('training_on_user_data', 'false'),
    ('plan', 'consumer'), ('zdr_enabled', False), ('zdr_required', None),
    ('policy_reference', ''), ('models', 'approved-model'), ('models', ['other-model']),
    ('endpoint', 'https://unapproved.example/v1/chat/completions'),
])
def test_ineligible_vendor_configuration_never_creates_transport(field, value):
    p = policy(); p[field] = value
    with patch('ai_client.build_opener') as transport:
        with pytest.raises(ValueError):
            make_client(policy=p)
        transport.assert_not_called()


def test_external_ai_requires_explicit_confirmation_and_policy():
    with pytest.raises(ValueError, match='Confirm approved'):
        make_client(approved_ai_use=False)
    with pytest.raises(ValueError, match='policy file'):
        make_client(policy=None)


def test_http_is_rejected_for_a_remote_provider():
    p = policy(); p['endpoint'] = 'http://approved.example/v1/chat/completions'
    with pytest.raises(ValueError, match='HTTPS'):
        AIClient(p['endpoint'], 'approved-model', policy=p, approved_ai_use=True)


def test_disabled_optional_zdr_does_not_change_no_training_requirement():
    p = policy(); p.update(zdr_required=False, zdr_enabled=False)
    assert make_client(policy=p)
    p['training_on_user_data'] = True
    with pytest.raises(ValueError, match='training'):
        make_client(policy=p)


def test_wire_format_is_provider_neutral_and_error_body_is_not_disclosed():
    client = make_client(api_key='fake-test-key')
    response = io.BytesIO(json.dumps({'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}], 'usage': {}}).encode())
    with patch.object(client.opener, 'open', return_value=response) as send:
        assert client.complete('policy', 'synthetic document')[0] == '{}'
        request = send.call_args.args[0]
        body = json.loads(request.data)
        assert body['model'] == 'approved-model'
        assert body['messages'][1]['content'] == 'synthetic document'
        assert 'thinking' not in body
    failure = HTTPError(client.endpoint, 403, 'secret excerpt', {}, io.BytesIO(b'sensitive record'))
    with patch.object(client.opener, 'open', side_effect=failure):
        with pytest.raises(RuntimeError) as error:
            client.complete('policy', 'synthetic')
        assert str(error.value) == 'AI provider returned HTTP 403'


@pytest.mark.parametrize('body', ['[]', '{}', '{"risk_level":"LOW"}', 'not JSON',
                                 '{"risk_level":["LOW"]}'])
def test_invalid_model_assessment_cannot_be_classified_low(body):
    client = type('FakeClient', (), {'complete': lambda *args: (body, {})})()
    result = _assess_record(client, {'text': 'synthetic', 'metadata': {'source': 'anon_test'}})
    assert result['risk_level'] == 'PARSE_ERROR'
    assert 'raw_response' not in result


def test_failed_and_sampled_assessments_do_not_clear_unchecked_records():
    report = {'assessments': [{'risk_level': 'ERROR'}], 'total_records_in_dataset': 1}
    _summarize(report)
    assert report['highest_risk_level'] == 'UNKNOWN'
    assert not report['assessment_complete']
    report = {'assessments': [{'risk_level': 'LOW'}], 'total_records_in_dataset': 2}
    _summarize(report)
    assert report['highest_risk_level'] == 'UNKNOWN'


def test_retry_keeps_distinct_chunks_of_the_same_document(tmp_path):
    dataset = tmp_path / 'finetune/dataset.jsonl'
    dataset.parent.mkdir()
    records = [{'text': f'chunk {i}', 'metadata': {'source': 'anon_test', 'chunk_index': i}} for i in range(2)]
    dataset.write_text(''.join(json.dumps(r) + '\n' for r in records))
    report = {'total_records_in_dataset': 2, 'dataset_sha256': sha256_file(dataset),
              'assessments': [{'_source': 'anon_test', '_record_id': f'anon_test:{i}', 'risk_level': 'ERROR'} for i in range(2)]}
    (tmp_path / 're_id_risk_report.json').write_text(json.dumps(report))
    def assess(client, record, model):
        return {'_source': 'anon_test', '_record_id': f"anon_test:{record['metadata']['chunk_index']}", 'risk_level': 'LOW'}
    with patch('re_id_risk._make_client'), patch('re_id_risk._assess_record', side_effect=assess) as call:
        result = retry_errors(tmp_path, quiet=True)
        assert call.call_count == 2
    assert [a['_record_id'] for a in result['assessments']] == ['anon_test:0', 'anon_test:1']
    assert result['assessment_complete']


def test_redirect_cannot_forward_document_text_or_credentials():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    received = []
    class Destination(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.path)
            self.send_response(200); self.end_headers()
        def log_message(self, *args): pass
    destination = HTTPServer(('127.0.0.1', 0), Destination)
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(307)
            self.send_header('Location', f'http://127.0.0.1:{destination.server_port}/unexpected')
            self.end_headers()
        def log_message(self, *args): pass
    origin = HTTPServer(('127.0.0.1', 0), Redirect)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in [destination, origin]]
    for thread in threads: thread.start()
    try:
        p = policy(); p['endpoint'] = f'http://127.0.0.1:{origin.server_port}/chat/completions'
        client = AIClient(p['endpoint'], 'approved-model', 'fake-key', policy=p, approved_ai_use=True)
        with pytest.raises(RuntimeError, match='HTTP 307'):
            client.complete('test instructions', 'synthetic legal text')
        assert received == []
    finally:
        for server in [origin, destination]: server.shutdown(); server.server_close()
        for thread in threads: thread.join(timeout=2)
