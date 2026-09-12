"""One opt-in Cloudflare Qwen routing call for synthetic QC002; no deployment.

Default: prepare and validate the request locally, without reading credentials.
Live: require explicit account and Free-plan acknowledgement; no retry/fallback.
This is not the complete web workflow or a model-accuracy evaluation.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from clinical_qc_demo.data import load_knowledge, load_records
from clinical_qc_demo.local_model import decode_json
from clinical_qc_demo.m4_engine import clauses, context_only, evaluate_issue, validate_groups
from clinical_qc_demo.m4_workflow import PROMPT_VERSION, decode_routing

MODEL = '@cf/qwen/qwen3-30b-a3b-fp8'
WRANGLER = ROOT.parent / 'node_modules/wrangler/bin/wrangler.js'


def prepare():
    record = next(r for r in load_records(ROOT) if r.case_id == 'QC002')
    parts = clauses(record.text)
    context = [p['clause_id'] for p in parts if context_only(p['quote'])]
    facts = [str(p['clause_id']) for p in parts if p['clause_id'] not in context]
    # Reuse the current routing prompt without changing the local workflow.
    source = ROOT / 'src/clinical_qc_demo/m4_workflow.py'
    prompts = [n.value.value for n in ast.walk(ast.parse(source.read_text()))
               if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
               and isinstance(n.value.value, str)
               and any(isinstance(t, ast.Name) and t.id == 'prompt' for t in n.targets)]
    if len(prompts) != 1 or not facts:
        raise ValueError('Cannot unambiguously locate the existing routing contract.')
    item = {'type': 'object', 'properties': {
        'family': {'enum': ['pk', 'visit', 'ae', 'drug', 'role', 'edc', 'unknown']},
        'group': {'type': 'integer', 'minimum': 1, 'maximum': 8}},
        'required': ['family', 'group'], 'additionalProperties': False}
    schema = {'type': 'object', 'properties': {k: item for k in facts},
              'required': facts, 'additionalProperties': False}
    payload = {'messages': [
        {'role': 'system', 'content': prompts[0] + '\n只返回最终的紧凑JSON，不写Markdown。/no_think'},
        {'role': 'user', 'content': json.dumps({
            'clauses': parts, 'context_clause_ids': context,
            'required_clause_ids': facts}, ensure_ascii=False)}],
        'response_format': {'type': 'json_schema', 'json_schema': schema},
        'stream': False, 'max_tokens': 512, 'temperature': 0, 'seed': 42}
    return record, parts, context, facts, payload


def validate_answer(raw, record, parts, context, facts):
    proposal = decode_routing(raw, facts, context)
    groups = validate_groups(parts, proposal)
    taxonomy, _, rules = load_knowledge(ROOT)
    issues = [evaluate_issue(record, g, context, parts, rules, taxonomy, i)
              for i, g in enumerate(groups, 1)]
    # Assert this single fixture's known outcome only AFTER inference.
    # These checks and rule answers are never included in the model request.
    checks = {'one_pk_issue': len(issues) == 1 and issues[0]['family'] == 'pk',
              'complete_partition': True,
              'expected_l3': [f['l3_id'] for i in issues for f in i['findings']] == ['L3-PK-001'],
              'expected_calculation': len(issues) == 1 and
                  bool(issues[0]['calculation']) and
                  Decimal(issues[0]['calculation'].get('actual_minutes', 'NaN')) == Decimal(27) and
                  issues[0]['calculation'].get('minimum_minutes') == 30}
    return {'decomposition': proposal.model_dump(), 'issues': issues,
            'checks': checks, 'passed': all(checks.values())}


def credential():
    node = shutil.which('node')
    if not node or not WRANGLER.is_file():
        raise ValueError('Existing Wrangler installation was not found.')
    # Wrangler can log command output. Confine auth-export logs to a private
    # temporary directory and remove them immediately; never log the token here.
    with tempfile.TemporaryDirectory(prefix='qc-cloud-auth-') as private:
        env = dict(os.environ, WRANGLER_SEND_METRICS='false',
                   WRANGLER_LOG_PATH=str(Path(private) / 'auth.log'))
        proc = subprocess.run([node, str(WRANGLER), 'auth', 'token', '--json'],
                              capture_output=True, text=True, timeout=30, env=env,
                              cwd=ROOT)
        if proc.returncode:
            raise ValueError('Cloudflare credential lookup failed; details suppressed.')
        data = decode_json(proc.stdout)
        if data.get('type') != 'oauth' or not isinstance(data.get('token'), str):
            raise ValueError('Expected the user-approved Wrangler OAuth login.')
        return data['token']


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected redirect rejected; no token forwarded.')


def run_once(account, payload, report):
    endpoint = f'https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}'
    request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={'Authorization': 'Bearer ' + credential(),
                               'Content-Type': 'application/json'})
    started = perf_counter()
    report['request_count'] = 1
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=90) as response:
            report['http_status'] = response.status
            raw = response.read(1_048_577)
    except HTTPError as error:
        report['http_status'] = error.code
        raw = error.read(8192)
    finally:
        report['network_wall_ms'] = round((perf_counter() - started) * 1000, 3)
    if len(raw) > 1_048_576:
        raise ValueError('Response exceeded the size limit.')
    envelope = decode_json(raw.decode())
    report['provider_response'] = envelope
    if report['http_status'] != 200 or envelope.get('success') is not True:
        raise ValueError('Cloudflare rejected the request; no retry or fallback.')
    result = envelope['result']
    if result.get('model') not in (None, MODEL):
        raise ValueError('Provider returned a different model; output rejected.')
    report['usage'] = result.get('usage')  # Missing is unknown, never zero.
    if isinstance(result.get('choices'), list) and len(result['choices']) == 1:
        choice = result['choices'][0]
        report['finish_reason'] = choice.get('finish_reason')
        if choice.get('finish_reason') != 'stop':
            raise ValueError('Generation did not finish normally; partial output rejected.')
        message = choice.get('message', {})
        if message.get('role') != 'assistant' or message.get('tool_calls'):
            raise ValueError('Unexpected message role or tool call rejected.')
        content = message.get('content')
        return decode_json(content) if isinstance(content, str) else content
    # Native JSON mode can return an already parsed response object.
    if 'response' in result:
        if result.get('finish_reason') not in (None, 'stop'):
            raise ValueError('Native response was truncated or incomplete.')
        content = result['response']
        return decode_json(content) if isinstance(content, str) else content
    raise ValueError('Unknown provider response shape.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--account-id')
    parser.add_argument('--confirmed-workers-free', action='store_true')
    args = parser.parse_args()
    record, parts, context, facts, payload = prepare()
    if not args.live:
        sample = {k: {'family': 'pk', 'group': 1} for k in facts}
        assert validate_answer(sample, record, parts, context, facts)['passed']
        try:
            validate_answer({}, record, parts, context, facts)
        except ValueError:
            pass
        else:
            raise AssertionError('Missing routing must be rejected.')
        print(json.dumps({'mode': 'offline_preflight', 'model_calls': 0,
                          'case_id': record.case_id, 'fact_clause_ids': facts,
                          'max_output_tokens': payload['max_tokens'], 'checks_passed': True},
                         ensure_ascii=False))
        return 0
    if not args.confirmed_workers_free or not re.fullmatch(r'[0-9a-f]{32}', args.account_id or ''):
        parser.error('Live execution requires account ID and a verified Workers Free plan.')
    report = {'test': 'cloud-qwen-qc002-routing-smoke-v1', 'synthetic': True,
              'model': MODEL, 'mode': 'live_cloud', 'prompt_version': PROMPT_VERSION + '+no_think',
              'started_at': datetime.now(timezone.utc).isoformat(), 'request_count': 0,
              'case_id': record.case_id, 'input': record.model_dump(), 'request': payload,
              'request_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
              'reference_file_read': False, 'explanation_generated': False,
              'deployment_performed': False, 'billing_changes': False, 'passed': False}
    try:
        answer = run_once(args.account_id, payload, report)
        report['routing_response'] = answer
        report.update(validate_answer(answer, record, parts, context, facts))
    except Exception as error:
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    output = ROOT / 'runtime/cloud-smoke' / (str(uuid4()) + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: report.get(k) for k in
                      ('passed', 'request_count', 'http_status', 'network_wall_ms',
                       'usage', 'finish_reason', 'checks', 'error')}, ensure_ascii=False))
    print('Report: ' + str(output))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
