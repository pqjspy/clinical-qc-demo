"""Bounded owner-side Workers AI client; never print or save authentication.

Used only by explicit synthetic live checks. Runtime Worker uses its AI binding,
not this CLI helper. Credentials remain in memory and are sent only to the
fixed Cloudflare API host, without redirects, retries or proxy forwarding.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_MODELS = {'@cf/baai/bge-m3', '@cf/baai/bge-reranker-base', '@cf/qwen/qwen3-30b-a3b-fp8'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Cloudflare redirect rejected; no credential forwarded.')


def credential():
    node = shutil.which('node')
    wrangler = ROOT/'cloud/node_modules/wrangler/bin/wrangler.js'
    if not node or not wrangler.is_file():
        raise ValueError('The pinned local Wrangler installation is required.')
    with tempfile.TemporaryDirectory(prefix='qc-retrieval-auth-') as private:
        env = dict(os.environ, WRANGLER_SEND_METRICS='false', WRANGLER_LOG_PATH=str(Path(private)/'auth.log'))
        result = subprocess.run([node, str(wrangler), 'auth', 'token', '--json'], cwd=ROOT/'cloud',
            env=env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Authorized Cloudflare login could not be read; details suppressed.')
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise ValueError('Unexpected auth response; details suppressed.') from None
        if payload.get('type') != 'oauth' or not isinstance(payload.get('token'), str):
            raise ValueError('Expected the previously authorized OAuth account.')
        return payload['token']


class Client:
    def __init__(self, account_id, *, confirmed_workers_free=False, max_requests=36):
        if confirmed_workers_free is not True or not re.fullmatch(r'[0-9a-f]{32}', account_id or ''):
            raise ValueError('Live tests require the exact account and current Workers Free confirmation.')
        if type(max_requests) is not int or not 1 <= max_requests <= 50:
            raise ValueError('Bound the owner-side test to 1..50 model calls.')
        self.account_id, self.max_requests = account_id, max_requests
        self._token = None
        self.calls = []

    def run(self, model, payload):
        if model not in ALLOWED_MODELS or len(self.calls) >= self.max_requests:
            raise ValueError('Unapproved model or model-call budget reached.')
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(data) > 60000:
            raise ValueError('Owner-side model request exceeds 60 KB budget.')
        if self._token is None:
            self._token = credential()
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{model}'
        request = Request(url, data=data, headers={'Authorization': 'Bearer '+self._token, 'Content-Type': 'application/json'})
        metadata = {'model': model, 'status': 'started'}
        self.calls.append(metadata)
        start = perf_counter()
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
                metadata['http_status'] = response.status
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Cloudflare response exceeded the size limit.')
            envelope = json.loads(raw)
            if envelope.get('success') is not True or not isinstance(envelope.get('result'), dict):
                raise ValueError('Cloudflare rejected inference; no fallback.')
            result = envelope['result']
            metadata.update(status='completed', usage=result.get('usage'))
            return result
        except HTTPError as exc:
            metadata.update(status='failed', http_status=exc.code)
            raise ValueError(f'Cloudflare HTTP {exc.code}; no retry or paid fallback.') from None
        except Exception as exc:
            metadata.update(status='failed', error_type=type(exc).__name__)
            raise ValueError('Cloudflare inference failed; no retry or paid fallback.') from None
        finally:
            metadata['wall_ms'] = round((perf_counter()-start)*1000, 3)
