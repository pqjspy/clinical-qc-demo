# Synthetic retrieval comparison, version 2

This is a small engineering check of rule retrieval, not a clinical evaluation.
The 18 Chinese queries in `queries.json` were newly authored by Codex from the
repository's synthetic rule quotations and applicability contracts on 2026-09-14.
No existing case answers, private runtime records, patient data, job-search
material or model outputs were used to author the queries or labels. The
queries are separate from earlier demonstration cases, but their author saw the
same rules that will be retrieved. Labels have not been independently adjudicated.

The fixed split is 6 development queries (001–006) and 12 evaluation queries
(007–018), frozen before model calls. The development set covers one literal
query for each of PK, visits, adverse-event recording, drug inventory, personnel
authorization and original-record/EDC alignment. The evaluation set contains six
paraphrases, three two-rule queries, two version/date probes and one absent-study
probe. This deliberate split is not a random sample. Changing a query, label or
split after seeing results requires a new dataset version and disclosure of the
previous run; do not remove difficult queries or failures.

## Candidate corpus and labels

The corpus is `data/knowledge/rules.json`, whose original byte SHA-256 is stored
in `queries.json`. Its 9 source rows are entire `rule.source.quote` documents.
The labels use the actual contract's `Rule.key`: `rule_id@version`, including
`SYN-RULE-PK-001@1` and `SYN-RULE-PK-001@2` as distinct documents.

All methods must first apply the same exact study, site and protocol filters and
the rule's `[effective_from, effective_to)` interval using each query's explicit
timezone-aware `context.event_at`. The ordinary v1 queries have only 6 applicable
documents. The v2 boundary probe has only PK version 2: protocol/date filtering
removes old version 1 and all other families before ranking. The conflict-study
documents are outside these queries' scopes. The absent-study query has no
applicable documents and no reference targets. These very small pools make many
queries easy; the v2 probe primarily checks filtering, not retrieval quality.

Relevance is binary: a reference rule is the rule needed to answer the retrieval
question in its supplied context. It does not mean that the facts establish a
finding or that a proposed clinical action is correct. Both listed references
are equally relevant in a two-rule query. Missing evidence can still require a
relevant rule, as in the incomplete adverse-event register query.

`reference_ids`, `rationale`, `families`, `style`, `split` and the synthetic query
tracking IDs are offline evaluator metadata. Do not place them in embeddings,
reranker prompts, QC model inputs, runtime lookup logic, or the cloud bundle.
Versioned candidate document IDs may accompany results as ordinary retrieval
identifiers, but the relevance labels remain evaluator-only.

## Paired retrieval run

Run BM25, dense, hybrid and hybrid plus reranking (`bm25`, `dense`, `hybrid`,
`hybrid_reranked`) for every frozen query using the same text, scope and source
quotations. Preserve native ordering and record any deterministic tie breaker.
Any tuning uses development queries only. Before evaluating the frozen split,
record corpus and dataset hashes, model identifiers, document/query encoding
settings, BM25 parameters/tokenizer, hybrid fusion settings, reranking candidate
depth and prompt/configuration, relevant code version, timestamps and retry
policy. A reranker must receive all candidates allowed by its predeclared depth,
with original rule text; it must not see labels or the authored rationale.

For a scope with zero documents, return a successful empty ranking without
pretending a model call was made. Retain errors and partial results in the raw
run artifact; a failed method/query remains in the evaluation denominator with
zero target metric contribution. Apply the same declared retry policy to every
query. Report actual completed calls and any model/provider failure; do not
replace missing dense/reranker outputs with lexical results and describe them as
a successful model run. A failed call should not erase independent methods'
successful results for that query.

`scripts/retrieval_metrics.py` exposes pure functions `score_ranking` and
`evaluate_rows`. It performs no network, model, file or runtime-history reads.
The live evaluator supplies rows in this form after retrieval is complete:

```json
{
  "query_id": "SYN-RET2-013",
  "split": "eval",
  "reference_ids": ["SYN-RULE-PK-001@1", "SYN-RULE-EDC-001@1"],
  "rankings": {
    "bm25": ["SYN-RULE-PK-001@1", "SYN-RULE-EDC-001@1"],
    "dense": [],
    "hybrid": [],
    "hybrid_reranked": []
  },
  "status": {"bm25": "ok", "dense": "failed", "hybrid": "failed", "hybrid_reranked": "failed"}
}
```

The example is an input-schema illustration, not an observed result. Omitted
status means `ok`; any non-`ok` status string is a failure. A string status
applies to all methods, while a mapping applies independently. A missing method
ranking is a failure; an explicitly empty list with `ok` status is a valid
result. Duplicate query IDs are rejected. The caller must check that every
frozen query occurs exactly once because the metric function cannot infer an
omitted query. Metric output contains `splits.all` and separate `splits.dev` and
`splits.eval` aggregates, with the same set of query rows for every method.

## Metric definitions and reporting

For a query with nonempty relevant set R, Recall@k is the number of distinct
relevant documents in the first k ranked positions divided by |R|. Report
Recall@1 and Recall@3. MRR uses the reciprocal position of the first relevant
document in the complete returned ranking, or zero if absent. Binary nDCG@3 is
the sum of `1/log2(rank+1)` for distinct relevant documents in the first three
positions, divided by the ideal gain for `min(3, |R|)` documents. Average each
metric over all target-bearing queries, including failed runs as zero. A query
with two references therefore cannot have Recall@1 greater than 0.5.

Duplicate reference IDs denote one target. Duplicate ranked IDs consume their
original positions and can earn gain only once; they are never compressed into
better positions. Unrecognized returned IDs are nonrelevant. Do not silently
truncate full-list MRR to three results. A metric with no eligible denominator
is represented by JSON null (`None` in Python), not a success or failure score.

The evaluation split has 11 target-bearing queries plus 1 no-target query. The
no-target query is excluded from Recall, MRR and nDCG denominators and is reported
separately per method: query count, failures, successful empty and nonempty
rankings, and empty-ranking rate over all no-target queries. Failures do not
count as successful empty rankings. Each method also reports total failures
and target-query failures. Show evaluation results separately from development;
an overall aggregate is available only for transparency.

Record the per-query rankings and errors alongside the aggregate, including
multi-rule recall misses and any inappropriate scoped-out document. Report
paired metric differences descriptively. This dataset is too small and too
closely authored to support claims of general retrieval superiority, clinical
accuracy, clinical safety, production readiness, statistical significance or
generalization to new institutions, real patient records or larger corpora.
