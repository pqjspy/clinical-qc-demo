"""Real Workers AI dense/RRF/reranker context, separate from rule authority.

No model weights, answer labels, credentials, network client, or vector database
are embedded here. The caller supplies the existing authenticated AI binding.
"""
import asyncio
import hashlib
import json
import math
from time import perf_counter

from core.data import applicable, rule_conflicts
from core.hybrid_retrieval import build_comparison, apply_reranker

EMBEDDING_MODEL = '@cf/baai/bge-m3'
RERANKER_MODEL = '@cf/baai/bge-reranker-base'
RETRIEVAL_VERSION = 'cloud-retrieval-v2'
CANDIDATE_K = 8
FINAL_K = 4
RRF_K = 60
# The cross-encoder has a short context. Keep the old full-record workflow for
# long inputs instead of silently cropping away a second clinical issue.
MAX_ENHANCED_QUERY_CHARS = 256
MAX_DOCUMENT_CHARS = 256
MAX_DOCUMENTS = 32
MODEL_TIMEOUT = 18


def fingerprint(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def unpack_embeddings(response, expected_rows):
    if not isinstance(response, dict):
        raise ValueError('向量模型响应不是对象。')
    rows = response.get('data', response.get('response'))
    if not isinstance(rows, list) or len(rows) != expected_rows:
        raise ValueError('向量模型返回的条数与请求不一致。')
    shape = response.get('shape')
    if not isinstance(shape,list) or any(type(x) is not int for x in shape) or shape != [expected_rows, 1024]:
        raise ValueError('BGE-M3向量维度或顺序契约不符。')
    for row in rows:
        if not isinstance(row, list) or len(row) != 1024:
            raise ValueError('BGE-M3向量维度不符。')
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in row):
            raise ValueError('向量包含无效数值。')
        if not any(row):
            raise ValueError('拒绝零向量。')
    return rows


def unpack_reranking(response, candidate_ids):
    rows = response.get('response') if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) != len(candidate_ids):
        raise ValueError('重排模型没有返回所有候选的分数。')
    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('重排响应格式无效。')
        index, score = row.get('id'), row.get('score')
        if type(index) is not int or not 0 <= index < len(candidate_ids):
            raise ValueError('重排响应包含未知候选编号。')
        key = candidate_ids[index]
        if key in scores or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError('重排响应包含重复候选或无效分数。')
        scores[key] = score
    return scores


async def retrieve_context(record, registry, run_model, *, trace=None):
    """Compare four rankings over the same context-scoped, not family-scoped pool.

    At most one embedding batch + one cross-encoder request. No retries, hidden
    fallback, or changing rule validity based on scores. Long inputs explicitly
    retain the previous BM25-only issue workflow without being truncated.
    """
    if trace is None:
        trace = {}
    query = record.text
    rules = sorted((r for r in registry.rules if applicable(r, record)), key=lambda r: r.key)
    documents = [{'id': r.key, 'text': r.source.quote} for r in rules]
    trace.update(version=RETRIEVAL_VERSION, status='started', query=query,
        documents=documents, calls=[], selected_rules=[], used_in='not_used',
        configuration={'embedding_model': EMBEDDING_MODEL, 'reranker_model': RERANKER_MODEL,
            'candidate_k': CANDIDATE_K, 'final_k': FINAL_K, 'rrf_k': RRF_K,
            'query_sha256': fingerprint(query), 'documents_sha256': fingerprint(documents),
            'max_enhanced_query_chars': MAX_ENHANCED_QUERY_CHARS,
            'document_unit': 'entire_rule_source_quote', 'score_is_probability': False,
            'embedding_batch_policy': 'query_then_sorted_document_ids',
            'embedding_cache': False, 'automatic_retry': False},
        scope={'study_id': record.study_id, 'site_id': record.site_id,
            'protocol_id': record.protocol_id, 'event_at': record.event_at,
            'eligible_rule_ids': [r.key for r in rules], 'excluded_count': len(registry.rules)-len(rules),
            'conflicts': [list(pair) for pair in rule_conflicts(rules)],
            'family_filter_before_ranking': False})
    if not documents:
        trace.update(status='not_applicable', reason='当前上下文没有适用规则；不调用检索模型，不代表没有问题。')
        return trace
    if len(query) > MAX_ENHANCED_QUERY_CHARS or any(len(d['text']) > MAX_DOCUMENT_CHARS for d in documents):
        trace.update(status='not_applicable', reason='原文超过增强检索的256字短文本预算；保留完整原文，明确使用原有逐项BM25检查，不截断或冒称完成重排。', mode='legacy_bm25_only')
        return trace
    if len(documents) > MAX_DOCUMENTS:
        trace.update(status='failed',error='适用规则数超过增强检索预算；未调用模型。',error_type='ValueError',elapsed_ms=0)
        raise ValueError('适用规则数超过增强检索预算；不能静默截断知识库。')

    async def call(stage, model, payload):
        started = perf_counter()
        metadata = {'stage': stage, 'model': model, 'status': 'started',
                    'request_sha256': fingerprint(payload)}
        trace['calls'].append(metadata)
        try:
            response = await asyncio.wait_for(run_model(model, payload), timeout=MODEL_TIMEOUT)
            metadata.update(status='completed', response_sha256=fingerprint(response))
            if isinstance(response, dict) and isinstance(response.get('usage'), dict):
                metadata['usage'] = response['usage']
            return response
        except Exception as exc:
            metadata.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            metadata['wall_ms'] = round((perf_counter()-started)*1000, 3)

    started = perf_counter()
    try:
        response = await call('embedding', EMBEDDING_MODEL,
                              {'text': [query]+[d['text'] for d in documents], 'truncate_inputs': False})
        vectors = unpack_embeddings(response, len(documents)+1)
        comparison = build_comparison(query, documents, vectors[0],
            {d['id']: v for d, v in zip(documents, vectors[1:])},
            candidate_k=CANDIDATE_K, final_k=FINAL_K, rrf_k=RRF_K)
        trace['comparison'] = comparison
        lookup = {d['id']: d for d in documents}
        candidates = comparison['candidate_ids']
        response = await call('rerank', RERANKER_MODEL, {'query': query,
            'contexts': [{'text': lookup[key]['text']} for key in candidates], 'top_k': len(candidates)})
        scores = unpack_reranking(response, candidates)
        comparison = apply_reranker(comparison, scores)
        trace.update(comparison=comparison, status='completed',
                     selected_rules=[lookup[key] for key in comparison['selected_ids']])
        return trace
    except Exception as exc:
        trace.update(status='failed', error='增强检索未完成；没有伪造向量、重排分数或自动切换付费服务。', error_type=type(exc).__name__)
        if 'comparison' in trace:
            trace['comparison']['reranker_status'] = 'failed'
        raise
    finally:
        trace['elapsed_ms'] = round((perf_counter()-started)*1000, 3)
