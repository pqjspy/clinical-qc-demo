"""Run a fixed synthetic retrieval comparison; default is offline preflight.

Only query text and context-scoped rule quotations reach model inference.
References and splits are used afterwards for metrics, never for retrieval.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'cloud/src'))
from core.contracts import CaseRecord
from cloud_flow import RULES
from semantic_retrieval import retrieve_context, EMBEDDING_MODEL, RERANKER_MODEL
from retrieval_metrics import evaluate_rows, METHODS
from cf_retrieval_client import Client, credential


def load_fixture():
    path = ROOT/'data/evaluation/retrieval_v2/queries.json'
    fixture = json.loads(path.read_text())
    actual = hashlib.sha256((ROOT/fixture['corpus']['path']).read_bytes()).hexdigest()
    if actual != fixture['corpus']['sha256']:
        raise ValueError('Frozen evaluation corpus changed; use a new version.')
    ids = [q['query_id'] for q in fixture['queries']]
    if len(ids) != 18 or len(set(ids)) != 18:
        raise ValueError('Expected all 18 unique frozen queries.')
    return fixture, hashlib.sha256(path.read_bytes()).hexdigest()


async def run(fixture, client, report):
    for query in fixture['queries']:
        # Metadata/labels stay out of this CaseRecord and every inference call.
        record = CaseRecord.model_validate({'case_id': query['query_id'], 'text': query['text'], **query['context']})
        trace, error = {}, None
        try:
            async def call(model, payload):
                return await asyncio.to_thread(client.run, model, payload)
            await retrieve_context(record, RULES, call, trace=trace)
        except Exception as exc:
            error = type(exc).__name__
        rankings = {method: [row['document_id'] for row in values]
                    for method, values in trace.get('comparison', {}).get('rankings', {}).items()}
        if trace.get('status') == 'not_applicable' and not trace.get('documents'):
            rankings = {method: [] for method in METHODS}
        row = {'query_id': query['query_id'], 'split': query['split'],
               'reference_ids': query['reference_ids'], 'rankings': rankings,
               'status': {method: 'ok' if method in rankings else 'failed' for method in METHODS},
               'trace': trace, 'error_type': error}
        report['rows'].append(row)
        print(json.dumps({'query_id': query['query_id'], 'split': query['split'],
                          'status': trace.get('status'), 'model_calls': len(trace.get('calls', []))}), flush=True)
    report['metrics'] = evaluate_rows(report['rows'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--account-id')
    parser.add_argument('--confirmed-workers-free', action='store_true')
    args = parser.parse_args()
    fixture, checksum = load_fixture()
    if not args.live:
        print(json.dumps({'mode': 'offline_preflight', 'queries': len(fixture['queries']),
            'fixture_sha256': checksum, 'corpus_sha256': fixture['corpus']['sha256'],
            'max_model_calls': 36, 'model_calls': 0, 'models': [EMBEDDING_MODEL, RERANKER_MODEL]}))
        return 0
    client = Client(args.account_id, confirmed_workers_free=args.confirmed_workers_free, max_requests=36)
    # Authentication lookup is outside the per-inference timeout and not logged.
    client._token = credential()
    report = {'version': 'retrieval-v2-live-evaluation', 'synthetic': True,
              'fixture_sha256': checksum, 'corpus_sha256': fixture['corpus']['sha256'],
              'started_at': datetime.now(timezone.utc).isoformat(), 'rows': [],
              'reference_metadata_sent_to_models': False, 'billing_changes': False,
              'notes': ['Tiny scoped synthetic corpus; not clinical accuracy or large-corpus generalization.',
                        'Fixed pre-run settings; model failures stay in method denominators.']}
    folder = ROOT/'runtime/retrieval_v2'/str(uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    try:
        asyncio.run(run(fixture, client, report))
    finally:
        report.update(finished_at=datetime.now(timezone.utc).isoformat(), calls=client.calls,
                      total_model_calls=len(client.calls))
        (folder/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        print('Report: '+str(folder/'report.json'))
    print(json.dumps(report.get('metrics', {}), ensure_ascii=False, indent=2))
    return 0 if len(report['rows']) == 18 else 1


if __name__ == '__main__':
    raise SystemExit(main())
