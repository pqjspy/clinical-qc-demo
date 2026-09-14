"""Pure, offline retrieval metrics for authored synthetic relevance labels.

This module has no model, file, network, or runtime-reference access. Callers
provide ranked versioned rule IDs after each retrieval method has finished.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import math
from typing import Any

METHODS = ("bm25", "dense", "hybrid", "hybrid_reranked")
METRICS = ("recall_at_1", "recall_at_3", "mrr", "ndcg_at_3")


def _ids(values: Sequence[str], name: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{name} must be a list or tuple of nonempty string IDs.")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{name} contains an invalid ID.")
    return list(values)


def score_ranking(reference_ids: Sequence[str], ranked_ids: Sequence[str]) -> dict:
    """Binary relevance; return undefined metrics for queries with no targets.

    Reference duplicates describe one target. Ranking duplicates consume their
    original positions and receive gain only on their first occurrence; ranks
    are never compressed. Unknown IDs are ordinary nonrelevant retrieved items.
    MRR uses the complete supplied ranking; the other cutoffs are fixed at 1/3.
    """
    relevant = set(_ids(reference_ids, "reference_ids"))
    ranking = _ids(ranked_ids, "ranked_ids")
    if not relevant:
        return {metric: None for metric in METRICS}
    seen: set[str] = set()
    hit_ranks = []
    for rank, rule_id in enumerate(ranking, start=1):
        if rule_id in relevant and rule_id not in seen:
            hit_ranks.append(rank)
        seen.add(rule_id)
    ideal_dcg = sum(1.0 / math.log2(rank + 1)
                    for rank in range(1, min(3, len(relevant)) + 1))
    return {
        "recall_at_1": sum(rank <= 1 for rank in hit_ranks) / len(relevant),
        "recall_at_3": sum(rank <= 3 for rank in hit_ranks) / len(relevant),
        "mrr": 1.0 / hit_ranks[0] if hit_ranks else 0.0,
        "ndcg_at_3": sum(1.0 / math.log2(rank + 1) for rank in hit_ranks if rank <= 3)
        / ideal_dcg,
    }


def _method_failed(row: Mapping, method: str, rankings: Mapping) -> bool:
    status = row.get("status", "ok")
    if isinstance(status, Mapping):
        status = status.get(method, "ok")
    if not isinstance(status, str) or not status:
        raise ValueError("status must be a nonempty string or a method-to-string mapping.")
    # A missing method output is a failed run, never a silently omitted query.
    return status != "ok" or method not in rankings


def _aggregate(rows: list[dict], methods: tuple[str, ...]) -> dict:
    target_count = sum(bool(row["reference_ids"]) for row in rows)
    no_target_count = len(rows) - target_count
    aggregates = {}
    for method in methods:
        totals = {metric: 0.0 for metric in METRICS}
        failed_count = target_failed = no_target_failed = 0
        no_target_empty = no_target_nonempty = 0
        for row in rows:
            failed = row["failures"][method]
            failed_count += int(failed)
            if not row["reference_ids"]:
                if failed:
                    no_target_failed += 1
                elif row["rankings"][method]:
                    no_target_nonempty += 1
                else:
                    no_target_empty += 1
                continue
            if failed:
                target_failed += 1
                continue  # Zero contribution; target remains in denominator.
            scores = score_ranking(row["reference_ids"], row["rankings"][method])
            for metric in METRICS:
                totals[metric] += scores[metric]
        aggregates[method] = {
            **{metric: value / target_count if target_count else None
               for metric, value in totals.items()},
            "n_queries": len(rows),
            "n_target_queries": target_count,
            "n_failed": failed_count,
            "n_target_failed": target_failed,
            "no_target": {
                "n_queries": no_target_count,
                "n_failed": no_target_failed,
                "n_empty_rankings": no_target_empty,
                "n_nonempty_rankings": no_target_nonempty,
                # Failures stay in this denominator and do not count as empty.
                "empty_ranking_rate": no_target_empty / no_target_count
                if no_target_count else None,
            },
        }
    return {
        "n_queries": len(rows),
        "n_target_queries": target_count,
        "n_no_target_queries": no_target_count,
        "methods": aggregates,
    }


def evaluate_rows(rows: Iterable[Mapping[str, Any]], *,
                  methods: Sequence[str] = METHODS) -> dict:
    """Aggregate identical query rows by method, overall and within each split.

    Each row must contain query_id, split, reference_ids and a rankings mapping
    from method to ordered list[str]. status may be omitted/"ok", a non-ok
    failure string, or a per-method mapping of these strings. Any non-ok status
    or missing method ranking counts as failure. Even partial returned rankings
    receive zero target metrics when the method failed. A successful empty list
    is valid output, distinct from a missing output or failure.

    The caller must supply every frozen query; missing rows cannot be inferred
    here. Duplicate query IDs, malformed IDs/rankings and reserved split "all"
    are rejected. Returns None for zero metric denominators, never false 0/1.
    Does not mutate rows, labels or rankings.
    """
    selected_methods = tuple(_ids(methods, "methods"))
    if not selected_methods or len(set(selected_methods)) != len(selected_methods):
        raise ValueError("methods must be nonempty and unique.")
    normalized = []
    seen_queries = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("Each query row must be a mapping.")
        query_id = row.get("query_id")
        split = row.get("split")
        if not isinstance(query_id, str) or not query_id.strip():
            raise ValueError("Each row needs a nonempty query_id.")
        if query_id in seen_queries:
            raise ValueError(f"Duplicate query_id: {query_id}")
        seen_queries.add(query_id)
        if not isinstance(split, str) or not split.strip() or split == "all":
            raise ValueError("Each row needs a nonempty split other than reserved 'all'.")
        references = _ids(row["reference_ids"], "reference_ids")
        rankings = row.get("rankings", {})
        if not isinstance(rankings, Mapping):
            raise TypeError("rankings must be a method-to-ranking mapping.")
        outputs = {}
        failures = {}
        for method in selected_methods:
            failed = _method_failed(row, method, rankings)
            failures[method] = failed
            outputs[method] = [] if failed else _ids(rankings[method], f"rankings.{method}")
        normalized.append({"query_id": query_id, "split": split,
                           "reference_ids": references, "rankings": outputs,
                           "failures": failures})
    splits = {"all": _aggregate(normalized, selected_methods)}
    for split in sorted({row["split"] for row in normalized}):
        splits[split] = _aggregate([row for row in normalized if row["split"] == split],
                                  selected_methods)
    return {"method_names": list(selected_methods), "n_queries": len(normalized),
            "splits": splits}
