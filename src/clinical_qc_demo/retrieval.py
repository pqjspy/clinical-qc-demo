"""Small, deterministic BM25 retrieval over scoped synthetic rule quotations.

This module consumes in-memory input and approved knowledge. It never reads a
case's authored reference answer, calls a model, or performs file/network I/O.
Scores indicate lexical matches, not confidence, relevance proof or rule hits.
"""
from __future__ import annotations

from collections import Counter
import math
import re

from .contracts import CaseRecord, Rule, aware_time
from .data import applicable, rule_conflicts


K1 = 1.5
B = 0.75
_TOKEN_RUNS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Contiguous Han bigrams and lower-case ASCII words; no learned tokenizer.

    Han punctuation/ASCII delimit runs: ``采血，离心`` becomes [采血, 离心],
    never 血离. A one-character Han run produces no bigram. ASCII underscores
    stay inside words; hyphens delimit words. There is no stemming, synonym
    expansion, stop-word removal or case-answer lookup.
    """
    if not isinstance(text, str):
        raise TypeError("Retrieval text must be a string.")
    result = []
    for match in _TOKEN_RUNS.finditer(text):
        run = match.group(0)
        if run.isascii():
            result.append(run.lower())
        else:
            result.extend(run[index:index + 2] for index in range(len(run) - 1))
    return result


def retrieve_rules(record: CaseRecord, query: str, rules: list[Rule], *,
                   event_at: str | None = None) -> dict:
    """Filter applicability first, then rank every scoped rule's exact quote.

    ``event_at`` is an explicit caller-supplied event timestamp override, not a
    value inferred by this retriever. The caller must validate its provenance.
    Missing override uses record.event_at. Exact protocol IDs still apply: a
    timestamp override does not silently switch to a different protocol version.

    Document frequency and average length use only the applicable corpus. Query
    tokens are unique (binary query term frequency). Positive Robertson-style
    IDF is log(1 + (N - df + 0.5) / (df + 0.5)). Ties are resolved by rule.key.
    All positive-score candidates survive; zero-score rows remain in ranking.
    Conflicts use ALL applicable rules, including those with no lexical match.
    """
    query_terms = set(tokenize(query))
    selected_event = record.event_at if event_at is None else event_at
    aware_time(selected_event)  # Reject a naive/invalid override even if no rules.
    keys = [rule.key for rule in rules]
    if len(set(keys)) != len(keys):
        raise ValueError("Rule version keys must be unique before retrieval.")
    event_record = record.model_copy(update={"event_at": selected_event})
    scoped = sorted((rule for rule in rules if applicable(rule, event_record)),
                    key=lambda rule: rule.key)
    counts = [Counter(tokenize(rule.source.quote)) for rule in scoped]
    document_frequency = Counter(term for terms in counts for term in terms)
    lengths = [sum(terms.values()) for terms in counts]
    count = len(scoped)
    average_length = sum(lengths) / count if count else 0.0
    ranking = []
    for rule, terms, length in zip(scoped, counts, lengths):
        matched = sorted(query_terms.intersection(terms))
        score = 0.0
        if average_length:
            for term in matched:
                frequency = terms[term]
                df = document_frequency[term]
                inverse_frequency = math.log1p((count - df + 0.5) / (df + 0.5))
                normalizer = frequency + K1 * (1.0 - B + B * length / average_length)
                score += inverse_frequency * frequency * (K1 + 1.0) / normalizer
        ranking.append({"rule_key": rule.key, "score": score,
                        "matched_terms": matched})
    ranking.sort(key=lambda item: (-item["score"], item["rule_key"]))
    conflicts = [list(pair) for pair in sorted(rule_conflicts(scoped))]
    return {
        "configuration": {
            "method": "bm25", "version": "m2-bm25-v1", "k1": K1, "b": B,
            "tokenizer": "contiguous_han_bigrams_lowercase_ascii_words_v1",
            "query_term_frequency": "binary",
            "idf": "log(1 + (N - df + 0.5) / (df + 0.5))",
            "document_unit": "entire_rule_source_quote",
            "statistics_scope": "applicable_rules_only",
            "candidate_policy": "all_positive_scores_no_top_k",
            "tie_break": "rule_key_ascending",
            "applicable_document_count": count,
            "average_document_token_count": average_length,
            "score_meaning": "lexical_match_not_confidence_or_rule_hit",
            "conflict_scope": "all_applicable_rules_including_zero_scores",
        },
        "query": query,
        "event_at": selected_event,
        "applicable_rule_keys": [rule.key for rule in scoped],
        "ranking": ranking,
        "candidate_rule_keys": [item["rule_key"] for item in ranking if item["score"] > 0],
        "conflicts": conflicts,
    }
