"""Deterministic ranking over an already applicability-filtered document corpus.

Vectors and cross-encoder scores must be supplied by the caller. This module
does no model, network, or file I/O and never manufactures embeddings or scores.
Its scores measure retrieval ordering; they are not confidence or rule hits.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from numbers import Real

from .retrieval import B, K1, tokenize


MAX_CANDIDATE_K = 1000
MAX_RRF_K = 100000


def _bounded_integer(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}.")
    return value


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number, not a boolean.")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be a finite real number.") from error
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite real number.")
    return converted


def _unit_vector(vector: list[float], name: str, dimensions: int | None = None) -> list[float]:
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError(f"{name} must be a nonempty vector.")
    if dimensions is not None and len(vector) != dimensions:
        raise ValueError(f"{name} must have {dimensions} dimensions.")
    values = [_finite_number(value, name) for value in vector]
    scale = max(abs(value) for value in values)
    if not scale:
        raise ValueError(f"{name} must have nonzero norm.")
    # Scaling first avoids overflow/underflow for valid very large/small floats.
    scaled = [value / scale for value in values]
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    return [value / norm for value in scaled]


def _ranking(scores: dict[str, float]) -> list[dict]:
    ordered = sorted(scores, key=lambda document_id: (-scores[document_id], document_id))
    return [{"document_id": document_id, "rank": rank, "score": scores[document_id]}
            for rank, document_id in enumerate(ordered, start=1)]


def build_comparison(query: str, documents: list[dict], query_vector: list[float],
                     document_vectors: dict[str, list[float]], *, candidate_k: int = 8,
                     final_k: int = 4, rrf_k: int = 60) -> dict:
    """Return full BM25, cosine, and reciprocal-rank-fusion corpus rankings.

    BM25 uses the existing lexical tokenizer/constants and binary query term
    frequency. RRF adds ``1 / (rrf_k + rank)`` from each complete ranking, with
    one-based ranks. Candidate/final cutoffs affect only the returned ID lists.
    The caller owns applicability filtering; every supplied document is ranked.
    Empty corpora require an empty vector mapping and a valid query vector.
    """
    _bounded_integer(candidate_k, "candidate_k", MAX_CANDIDATE_K)
    _bounded_integer(final_k, "final_k", MAX_CANDIDATE_K)
    _bounded_integer(rrf_k, "rrf_k", MAX_RRF_K)
    if final_k > candidate_k:
        raise ValueError("final_k must not exceed candidate_k.")
    query_terms = set(tokenize(query))
    if not isinstance(documents, list):
        raise ValueError("documents must be a list.")
    text_by_id = {}
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Each document must contain a string id and text.")
        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Document IDs must be nonempty strings.")
        if document_id in text_by_id:
            raise ValueError("Document IDs must be unique.")
        if not isinstance(document.get("text"), str):
            raise ValueError("Document text must be a string.")
        text_by_id[document_id] = document["text"]
    if not isinstance(document_vectors, dict) or set(document_vectors) != set(text_by_id):
        raise ValueError("document_vectors must contain exactly the supplied document IDs.")
    unit_query = _unit_vector(query_vector, "query_vector")
    unit_documents = {
        document_id: _unit_vector(document_vectors[document_id],
                                  f"Vector for {document_id}", len(unit_query))
        for document_id in sorted(text_by_id)
    }

    counts = {document_id: Counter(tokenize(text_by_id[document_id]))
              for document_id in sorted(text_by_id)}
    document_frequency = Counter(term for terms in counts.values() for term in terms)
    lengths = {document_id: sum(terms.values()) for document_id, terms in counts.items()}
    count = len(counts)
    average_length = sum(lengths.values()) / count if count else 0.0
    lexical_scores = {}
    for document_id, terms in counts.items():
        score = 0.0
        if average_length:
            for term in sorted(query_terms.intersection(terms)):
                frequency = terms[term]
                df = document_frequency[term]
                inverse_frequency = math.log1p((count - df + 0.5) / (df + 0.5))
                normalizer = frequency + K1 * (1.0 - B + B * lengths[document_id] / average_length)
                score += inverse_frequency * frequency * (K1 + 1.0) / normalizer
        lexical_scores[document_id] = score
    bm25 = _ranking(lexical_scores)
    dense = _ranking({
        document_id: max(-1.0, min(1.0, math.fsum(left * right for left, right
                                               in zip(unit_query, vector))))
        for document_id, vector in unit_documents.items()
    })
    fusion_scores = {document_id: 0.0 for document_id in counts}
    for ranking in (bm25, dense):
        for row in ranking:
            fusion_scores[row["document_id"]] += 1.0 / (rrf_k + row["rank"])
    hybrid = _ranking(fusion_scores)
    return {
        "version": "retrieval-v2",
        "document_count": count,
        "config": {"candidate_k": candidate_k, "final_k": final_k, "rrf_k": rrf_k},
        "rankings": {"bm25": bm25, "dense": dense, "hybrid": hybrid},
        "candidate_ids": [row["document_id"] for row in hybrid[:candidate_k]],
        "selected_ids": [row["document_id"] for row in hybrid[:final_k]],
        "mode": "hybrid",
        "reranker_status": "not_run",
    }


def apply_reranker(comparison: dict, scores_by_id: dict[str, float]) -> dict:
    """Copy a comparison and order its candidate subset by supplied model scores.

    Every candidate must have exactly one finite real score. Larger scores rank
    first, with document-ID ties. Existing corpus rankings remain unchanged.
    """
    candidates = comparison["candidate_ids"]
    if (not isinstance(candidates, list)
            or any(not isinstance(item, str) or not item for item in candidates)
            or len(set(candidates)) != len(candidates)):
        raise ValueError("Comparison candidate IDs must be unique nonempty strings.")
    if not isinstance(scores_by_id, dict) or set(scores_by_id) != set(candidates):
        raise ValueError("Reranker scores must contain exactly the candidate IDs.")
    scores = {document_id: _finite_number(scores_by_id[document_id],
                                         f"Reranker score for {document_id}")
              for document_id in candidates}
    final_k = _bounded_integer(comparison["config"]["final_k"], "final_k", MAX_CANDIDATE_K)
    result = deepcopy(comparison)
    result["rankings"]["hybrid_reranked"] = _ranking(scores)
    result["selected_ids"] = [row["document_id"]
                              for row in result["rankings"]["hybrid_reranked"][:final_k]]
    result["mode"] = "hybrid_reranked"
    result["reranker_status"] = "completed"
    return result
