"""Explicit, provider-neutral HTTP adapter for approved red-team deployments."""
import ipaddress
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AIClient:
    """Chat Completions wire format; no vendor SDK, tools, or implicit endpoint."""

    def __init__(self, endpoint, model, api_key=None, *, policy=None, approved_ai_use=False, timeout=120):
        if not approved_ai_use:
            raise ValueError('Confirm approved Business/Enterprise AI use, training disabled, '
                             'and applicable retention/ZDR requirements before sending records')
        if not endpoint or not model:
            raise ValueError('Set RE_ID_API_URL and RE_ID_MODEL (or --api-url and --model)')
        if not isinstance(policy, dict):
            raise ValueError('A reviewed AI policy file is required; set --ai-policy or RE_ID_POLICY')
        models = policy.get('models')
        if not isinstance(models, list) or not models or not all(isinstance(m, str) and m for m in models):
            raise ValueError('The AI policy must list explicitly approved models')
        if policy.get('endpoint') != endpoint or model not in models:
            raise ValueError('AI endpoint/model is not approved by the policy file')
        if not isinstance(policy.get('plan'), str) or policy['plan'] not in {'business', 'enterprise'}:
            raise ValueError('The AI policy must require a Business or Enterprise-level agreement')
        if policy.get('training_on_user_data') is not False:
            raise ValueError('Refusing AI use: policy must explicitly prohibit training on user data')
        if not isinstance(policy.get('policy_reference'), str) or not policy['policy_reference'].strip():
            raise ValueError('Record the reviewed vendor contract/settings in policy_reference')
        if type(policy.get('zdr_required')) is not bool or type(policy.get('zdr_enabled')) is not bool:
            raise ValueError('The AI policy must explicitly record the applicable ZDR decision')
        if policy['zdr_required'] and not policy['zdr_enabled']:
            raise ValueError('Refusing AI use: Zero Data Retention is required but not enabled')
        parsed = urlsplit(endpoint)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback)) or not parsed.hostname:
            raise ValueError('AI endpoint must use HTTPS; HTTP is allowed only on loopback')
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Do not put credentials or fragments in the AI endpoint URL')
        if not 1 <= timeout <= 300:
            raise ValueError('API timeout must be between 1 and 300 seconds')
        self.endpoint, self.model, self.api_key, self.timeout = endpoint, model, api_key, timeout
        self.opener = build_opener(_NoRedirect())

    def complete(self, system, user):
        payload = {'model': self.model, 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'max_tokens': 8192, 'stream': False}
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        request = Request(self.endpoint, data=json.dumps(payload).encode(), headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('AI response exceeds the size limit')
            data = json.loads(raw)
            choice = data['choices'][0]
            if choice.get('finish_reason') not in ('stop', None):
                raise ValueError('AI response did not finish normally')
            text = choice['message']['content']
            if not isinstance(text, str):
                raise ValueError('AI response content must be text')
            return text, data.get('usage', {})
        except HTTPError as exc:
            # Provider error bodies and URLs may echo submitted records or keys.
            raise RuntimeError(f'AI provider returned HTTP {exc.code}') from None
        except (URLError, TimeoutError):
            raise RuntimeError('AI provider connection failed') from None
