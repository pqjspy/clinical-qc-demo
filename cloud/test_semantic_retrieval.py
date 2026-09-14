"""Offline provider substitutes: test contracts, not model retrieval accuracy."""
import asyncio
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'src'))
from bundled import BUNDLE
from cloud_flow import MODEL, RULES, analyze, validate_record
from core.m4_engine import clauses, context_only
from semantic_retrieval import (EMBEDDING_MODEL, MAX_DOCUMENTS, RERANKER_MODEL, retrieve_context,
                                unpack_embeddings, unpack_reranking)


def vector(index=0):
    row = [0.0]*1024
    row[index] = 1.0
    return row


class Provider:
    def __init__(self, fail=None, malformed=None):
        self.calls, self.fail, self.malformed = [], fail, malformed

    async def __call__(self, model, payload):
        self.calls.append((model, payload))
        if model == self.fail:
            raise TimeoutError()
        if model == EMBEDDING_MODEL:
            count = len(payload['text'])
            if self.malformed == 'embedding':
                return {'shape': [count, 1024], 'data': [[0.0]*1024]*count}
            return {'shape': [count, 1024], 'data': [vector(i) for i in range(count)]}
        if model == RERANKER_MODEL:
            ids = list(range(len(payload['contexts'])))
            if self.malformed == 'reranker':
                ids[-1] = ids[0]
            return {'response': [{'id': i, 'score': float(i)} for i in reversed(ids)]}
        required = payload['response_format']['json_schema']['required']
        if required[0].isdigit():
            result = {key: {'family': 'pk', 'group': 1} for key in required}
        else:
            result = {'I1': '同一份样本记录的采血和离心间隔为27分钟[I1-F2]，低于至少30分钟的合成规则要求[I1-R1]，需人工复核。'}
        return {'model': model, 'choices': [{'finish_reason': 'stop',
                 'message': {'role': 'assistant', 'content': json.dumps(result)}}]}


class SemanticTests(unittest.TestCase):
    def record(self, case='QC002'):
        return validate_record(next(c for c in BUNDLE['cases'] if c['case_id'] == case))

    def test_four_methods_same_scoped_documents_no_reference_labels(self):
        provider = Provider()
        trace = asyncio.run(retrieve_context(self.record(), RULES, provider))
        self.assertEqual(trace['status'], 'completed')
        self.assertEqual(len(trace['documents']), 6)
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(trace['scope']['family_filter_before_ranking'])
        self.assertNotIn('SYN-RULE-PK-001@2', trace['scope']['eligible_rule_ids'])
        for method, rows in trace['comparison']['rankings'].items():
            self.assertEqual({r['document_id'] for r in rows}, set(trace['scope']['eligible_rule_ids']), method)
        request = provider.calls[0][1]
        self.assertEqual(request['text'], [trace['query']]+[d['text'] for d in trace['documents']])
        self.assertIs(request['truncate_inputs'], False)
        self.assertEqual(trace['used_in'], 'not_used')
        self.assertNotIn('reference_ids', json.dumps(provider.calls))

    def test_real_pipeline_passes_selected_quotes_but_keeps_rule_decision(self):
        provider = Provider()
        result = asyncio.run(analyze(self.record(), 'offline-enhanced', provider))
        self.assertEqual(result['status'], 'proposed_findings', result)
        self.assertEqual(result['ai_call_count_total'], 4)
        self.assertEqual(result['model_call_count'], 2)
        self.assertEqual(result['retrieval_model_call_count'], 2)
        trace = result['retrieval_augmented']
        self.assertEqual(trace['used_in'], 'decomposition_context')
        routing = json.loads(provider.calls[2][1]['messages'][1]['content'])
        self.assertEqual(routing['retrieved_rule_context'], trace['selected_rules'])
        self.assertEqual(len(trace['selected_rules']), 4)
        self.assertEqual(Decimal(result['issues'][0]['calculation']['actual_minutes']), Decimal(27))
        self.assertEqual(result['findings'][0]['l3_id'], 'L3-PK-001')
        self.assertTrue(result['review_required'])

    def test_disabled_enhancement_restores_exact_legacy_routing_request(self):
        record, provider = self.record(), Provider()
        result = asyncio.run(analyze(record, 'offline-legacy', provider, enhanced_retrieval=False))
        self.assertEqual(result['status'], 'proposed_findings', result)
        self.assertEqual(result['prompt_version'], 'cloud-m4-routing-v1')
        self.assertNotIn('retrieval_augmented', result)
        self.assertEqual([model for model, _ in provider.calls], [MODEL, MODEL])
        self.assertEqual(result['retrieval_model_call_count'], 0)
        self.assertEqual(result['ai_call_count_total'], 2)
        parts = clauses(record.text)
        context = [part['clause_id'] for part in parts if context_only(part['quote'])]
        expected_input = {'clauses': parts, 'context_clause_ids': context,
                          'required_clause_ids': [str(part['clause_id']) for part in parts
                                                  if part['clause_id'] not in context]}
        messages = provider.calls[0][1]['messages']
        self.assertEqual(messages[0], {'role': 'system',
            'content': BUNDLE['routing_prompt']+'\n只返回紧凑JSON，不写Markdown。/no_think'})
        self.assertEqual(messages[1]['role'], 'user')
        self.assertEqual(json.loads(messages[1]['content']), expected_input)
        self.assertNotIn('retrieved_rule_context', messages[1]['content'])

    def test_bundle_hashes_match_cloud_implementation_and_original_hybrid_core(self):
        expected_cloud = {name: hashlib.sha256((ROOT/'src'/name).read_bytes()).hexdigest()
                          for name in ('cloud_flow.py', 'semantic_retrieval.py', 'entry.py', 'review.py')}
        self.assertEqual(BUNDLE['cloud_implementation_sha256'], expected_cloud,
                         'Regenerate the bundle after changing cloud implementation sources.')
        hybrid_name = 'hybrid_retrieval.py'
        original = ROOT.parent/'src'/'clinical_qc_demo'/hybrid_name
        bundled = ROOT/'src'/'core'/hybrid_name
        self.assertEqual(bundled.read_bytes(), original.read_bytes())
        self.assertEqual(BUNDLE['implementation_sha256'][hybrid_name],
                         hashlib.sha256(original.read_bytes()).hexdigest())

    def test_oversized_scope_fails_with_terminal_trace_before_any_model_call(self):
        base = RULES.rules[0]
        oversized = RULES.model_copy(update={'rules': [
            base.model_copy(update={'rule_id': f'SYN-BUDGET-{index:03}'})
            for index in range(MAX_DOCUMENTS+1)]})
        provider, trace = Provider(), {}
        with self.assertRaisesRegex(ValueError, '适用规则数超过增强检索预算'):
            asyncio.run(retrieve_context(self.record(), oversized, provider, trace=trace))
        self.assertEqual(trace['status'], 'failed')
        self.assertEqual(trace['error_type'], 'ValueError')
        self.assertIn('未调用模型', trace['error'])
        self.assertGreaterEqual(trace['elapsed_ms'], 0)
        self.assertEqual(len(trace['documents']), MAX_DOCUMENTS+1)
        self.assertEqual(trace['calls'], [])
        self.assertEqual(trace['selected_rules'], [])
        self.assertEqual(trace['used_in'], 'not_used')
        self.assertNotIn('comparison', trace)
        with patch('cloud_flow.RULES', oversized):
            result = asyncio.run(analyze(self.record(), 'oversized-scope', provider))
        self.assertEqual(result['status'], 'analysis_failed')
        self.assertEqual(result['retrieval_augmented']['status'], 'failed')
        self.assertEqual(result['retrieval_augmented']['used_in'], 'not_used')
        self.assertEqual(result['ai_call_count_total'], 0)
        self.assertEqual(result['findings'], [])
        self.assertEqual(provider.calls, [])

    def test_retrieval_not_marked_used_when_routing_request_fails_before_dispatch(self):
        provider = Provider()
        with patch.dict(BUNDLE, {'routing_prompt': 'x'*18001}):
            result = asyncio.run(analyze(self.record(), 'routing-budget', provider))
        self.assertEqual(result['status'], 'analysis_failed')
        self.assertIn('模型请求超出演示输入预算', result['error']['message'])
        self.assertEqual(result['retrieval_augmented']['status'], 'completed')
        self.assertEqual(result['retrieval_augmented']['used_in'], 'not_used')
        self.assertEqual(len(result['retrieval_augmented']['selected_rules']), 4)
        self.assertEqual(result['model_call_count'], 0)
        self.assertEqual([model for model, _ in provider.calls], [EMBEDDING_MODEL, RERANKER_MODEL])

    def test_reranking_cannot_resolve_conflicting_rules(self):
        provider = Provider()
        result = asyncio.run(analyze(self.record('DEMO-CONFLICT-01'), 'conflict', provider))
        self.assertEqual(result['status'], 'rule_conflict', result)
        self.assertEqual(len(result['retrieval_augmented']['scope']['conflicts']), 1)
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['ai_call_count_total'], 3)

    def test_provider_failures_do_not_fabricate_or_silently_fallback(self):
        for failure in (EMBEDDING_MODEL, RERANKER_MODEL):
            with self.subTest(model=failure):
                provider = Provider(fail=failure)
                result = asyncio.run(analyze(self.record(), 'failed', provider))
                self.assertEqual(result['status'], 'analysis_failed')
                self.assertEqual(result['model_call_count'], 0)
                trace = result['retrieval_augmented']
                self.assertEqual(trace['status'], 'failed')
                self.assertEqual(trace['used_in'], 'not_used')
                self.assertEqual(trace['selected_rules'], [])
                self.assertEqual(trace['calls'][-1]['status'], 'failed')
                self.assertGreaterEqual(trace['elapsed_ms'], 0)
                if failure == RERANKER_MODEL:
                    self.assertEqual(trace['comparison']['reranker_status'], 'failed')
                    self.assertNotIn('hybrid_reranked', trace['comparison']['rankings'])
                self.assertEqual(result['findings'], [])
                self.assertEqual(provider.calls[-1][0], failure)
                self.assertTrue(result['review_required'])
        for malformed in ('embedding', 'reranker'):
            with self.subTest(malformed=malformed):
                provider = Provider(malformed=malformed)
                result = asyncio.run(analyze(self.record(), 'invalid', provider))
                self.assertEqual(result['status'], 'analysis_failed')
                self.assertEqual(result['model_call_count'], 0)
                trace = result['retrieval_augmented']
                self.assertEqual(trace['status'], 'failed')
                self.assertEqual(trace['used_in'], 'not_used')
                self.assertEqual(trace['selected_rules'], [])
                self.assertEqual(trace['error_type'], 'ValueError')
                self.assertGreaterEqual(trace['elapsed_ms'], 0)
                self.assertTrue(all(model != MODEL for model, _ in provider.calls))

    def test_empty_scope_and_long_input_make_no_retrieval_model_calls(self):
        provider = Provider()
        records = [self.record().model_copy(update={'study_id': 'SYN-NO-STUDY'}),
                   self.record().model_copy(update={'text': self.record().text+'；补充文字'*70})]
        for record in records:
            trace = asyncio.run(retrieve_context(record, RULES, provider))
            self.assertEqual(trace['status'], 'not_applicable')
            self.assertEqual(trace['query'], record.text)
            self.assertEqual(trace['selected_rules'], [])
            self.assertEqual(trace['used_in'], 'not_used')
        self.assertEqual(provider.calls, [])

    def test_embedding_and_reranker_response_validation(self):
        valid = {'shape': [1, 1024], 'data': [vector()]}
        self.assertEqual(unpack_embeddings(valid, 1), valid['data'])
        bad = copy.deepcopy(valid); bad['data'][0][1] = float('nan')
        with self.assertRaises(ValueError): unpack_embeddings(bad, 1)
        bad = copy.deepcopy(valid); bad['data'][0][1] = True
        with self.assertRaises(ValueError): unpack_embeddings(bad, 1)
        with self.assertRaises(ValueError): unpack_embeddings(valid, 2)
        for response in ({'response': [{'id': True, 'score': 1}]},
                         {'response': [{'id': 0, 'score': float('inf')}]},
                         {'response': [{'id': 4, 'score': 1}]}):
            with self.assertRaises(ValueError): unpack_reranking(response, ['a'])
        self.assertEqual(unpack_reranking({'response': [{'id': 0, 'score': 0.0}]}, ['a']), {'a': 0.0})


if __name__ == '__main__':
    unittest.main(verbosity=2)
