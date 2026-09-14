"""Offline metric arithmetic and integrity of the newly authored query fixture."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from retrieval_metrics import METHODS, METRICS, evaluate_rows, score_ranking


class RankingMetricTests(unittest.TestCase):
    def test_first_rank_single_target(self):
        self.assertEqual(score_ranking(["A@1"], ["A@1", "B@1"]),
                         {metric: 1.0 for metric in METRICS})

    def test_multiple_references_and_position_discount(self):
        scores = score_ranking(["A@1", "B@1"], ["B@1", "X@1", "A@1"])
        self.assertEqual(scores["recall_at_1"], 0.5)
        self.assertEqual(scores["recall_at_3"], 1.0)
        self.assertEqual(scores["mrr"], 1.0)
        self.assertAlmostEqual(scores["ndcg_at_3"], 1.5 / (1 + 1 / math.log2(3)))

    def test_duplicate_references_and_rankings_do_not_create_gain_or_compress(self):
        scores = score_ranking(["A@1", "A@1", "B@1"],
                               ["X@1", "X@1", "A@1", "A@1", "B@1"])
        self.assertEqual(scores["recall_at_1"], 0.0)
        self.assertEqual(scores["recall_at_3"], 0.5)
        self.assertEqual(scores["mrr"], 1 / 3)
        self.assertAlmostEqual(scores["ndcg_at_3"], 0.5 / (1 + 1 / math.log2(3)))
        repeated_hit = score_ranking(["A@1", "B@1"], ["A@1", "A@1", "B@1"])
        self.assertAlmostEqual(repeated_hit["ndcg_at_3"],
                               1.5 / (1 + 1 / math.log2(3)))

    def test_mrr_uses_full_ranking_and_version_ids_are_distinct(self):
        scores = score_ranking(["A@2"], ["A@1", "X@1", "Y@1", "A@2"])
        self.assertEqual(scores, {"recall_at_1": 0.0, "recall_at_3": 0.0,
                                  "mrr": 0.25, "ndcg_at_3": 0.0})

    def test_recall_denominator_can_exceed_cutoff(self):
        scores = score_ranking(["A", "B", "C", "D"], ["A", "B", "C"])
        self.assertEqual(scores["recall_at_1"], 0.25)
        self.assertEqual(scores["recall_at_3"], 0.75)
        self.assertEqual(scores["ndcg_at_3"], 1.0)

    def test_no_target_is_undefined_and_empty_success_with_targets_is_zero(self):
        for ranking in ([], ["A"]):
            self.assertEqual(score_ranking([], ranking), {metric: None for metric in METRICS})
        self.assertEqual(score_ranking(["A"], []), {metric: 0.0 for metric in METRICS})

    def test_invalid_rank_shapes_fail_explicitly(self):
        for references, ranking in (("A", []), (["A"], "A"), (["A"], [None]),
                                    ([""], []), (["A"], [" "])):
            with self.assertRaises((TypeError, ValueError)):
                score_ranking(references, ranking)


class AggregateMetricTests(unittest.TestCase):
    @staticmethod
    def row(query_id, references, rankings, *, split="eval", **extra):
        return {"query_id": query_id, "split": split, "reference_ids": references,
                "rankings": rankings, **extra}

    def test_failures_remain_zero_in_same_target_denominator(self):
        rows = [
            self.row("Q1", ["A"], {method: ["A"] for method in METHODS}),
            self.row("Q2", ["A"], {method: ["A"] for method in METHODS},
                     status={"dense": "provider_error", "hybrid_reranked": "failed"}),
            self.row("Q3", ["A"], {"bm25": []}, status="failed"),
        ]
        before = deepcopy(rows)
        result = evaluate_rows(rows)["splits"]["eval"]
        self.assertEqual(rows, before)
        self.assertEqual(result["n_target_queries"], 3)
        for method in ("bm25", "hybrid"):
            self.assertAlmostEqual(result["methods"][method]["mrr"], 2 / 3)
            self.assertEqual(result["methods"][method]["n_failed"], 1)
        for method in ("dense", "hybrid_reranked"):
            self.assertAlmostEqual(result["methods"][method]["mrr"], 1 / 3)
            self.assertEqual(result["methods"][method]["n_failed"], 2)
            self.assertEqual(result["methods"][method]["n_target_failed"], 2)

    def test_missing_method_output_is_failure_but_empty_output_is_success(self):
        result = evaluate_rows([self.row("Q1", ["A"], {"bm25": []})])["splits"]["all"]
        self.assertEqual(result["methods"]["bm25"]["n_failed"], 0)
        self.assertEqual(result["methods"]["dense"]["n_failed"], 1)
        self.assertEqual(result["methods"]["dense"]["recall_at_3"], 0.0)

    def test_no_target_cases_are_separate_and_failed_is_not_empty(self):
        rows = [self.row("Q1", ["A"], {"bm25": ["A"]}),
                self.row("Q2", [], {"bm25": []}),
                self.row("Q3", [], {"bm25": ["X"]}),
                self.row("Q4", [], {"bm25": []}, status="timeout")]
        result = evaluate_rows(rows, methods=["bm25"])["splits"]["eval"]
        self.assertEqual(result["n_target_queries"], 1)
        self.assertEqual(result["n_no_target_queries"], 3)
        method = result["methods"]["bm25"]
        self.assertEqual(method["mrr"], 1.0)
        self.assertEqual(method["n_failed"], 1)
        self.assertEqual(method["n_target_failed"], 0)
        self.assertEqual(method["no_target"], {
            "n_queries": 3, "n_failed": 1, "n_empty_rankings": 1,
            "n_nonempty_rankings": 1, "empty_ranking_rate": 1 / 3})

    def test_split_reporting_and_empty_denominators(self):
        rows = [self.row("D1", ["A"], {"bm25": ["A"]}, split="dev"),
                self.row("E1", [], {"bm25": []})]
        result = evaluate_rows(rows, methods=["bm25"])
        self.assertEqual(result["n_queries"], 2)
        self.assertEqual(set(result["splits"]), {"all", "dev", "eval"})
        self.assertIsNone(result["splits"]["eval"]["methods"]["bm25"]["mrr"])
        self.assertIsNone(result["splits"]["dev"]["methods"]["bm25"]["no_target"]["empty_ranking_rate"])
        empty = evaluate_rows([])
        self.assertEqual(empty["splits"]["all"]["n_queries"], 0)
        self.assertIsNone(empty["splits"]["all"]["methods"]["bm25"]["recall_at_1"])

    def test_duplicate_queries_reserved_split_and_malformed_inputs_rejected(self):
        row = self.row("Q1", ["A"], {"bm25": ["A"]})
        with self.assertRaises(ValueError):
            evaluate_rows([row, row])
        with self.assertRaises(ValueError):
            evaluate_rows([{**row, "split": "all"}])
        with self.assertRaises(ValueError):
            evaluate_rows([row], methods=["bm25", "bm25"])
        with self.assertRaises(ValueError):
            evaluate_rows([{**row, "status": {"bm25": None}}])
        with self.assertRaises(TypeError):
            evaluate_rows([{**row, "rankings": {"bm25": None}}])


class RetrievalFixtureTests(unittest.TestCase):
    def test_frozen_fixture_scope_references_and_coverage(self):
        fixture = json.loads((ROOT / "data/evaluation/retrieval_v2/queries.json").read_text())
        source = (ROOT / "data/knowledge/rules.json").read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(), fixture["corpus"]["sha256"])
        rules = json.loads(source)["rules"]
        rule_keys = {f"{rule['rule_id']}@{rule['version']}" for rule in rules}
        queries = fixture["queries"]
        self.assertTrue(fixture["synthetic"])
        self.assertTrue(fixture["split_policy"]["frozen_before_model_run"])
        self.assertEqual(len(queries), 18)
        self.assertEqual(len({query["query_id"] for query in queries}), 18)
        self.assertEqual(len({query["text"] for query in queries}), 18)
        for split, expected_count in (("dev", 6), ("eval", 12)):
            selected = [query for query in queries if query["split"] == split]
            self.assertEqual(len(selected), expected_count)
            self.assertEqual([query["query_id"] for query in selected],
                             fixture["split_policy"][f"{split}_query_ids"])
            self.assertEqual({family for query in selected for family in query["families"]},
                             {"PK", "VISIT", "AE", "DRUG", "ROLE", "EDC"})
        no_target = []
        for query in queries:
            context = query["context"]
            event = datetime.fromisoformat(context["event_at"])
            self.assertIsNotNone(event.utcoffset())
            self.assertTrue(query["text"].startswith("【合成检索问题】"))
            self.assertTrue(set(query["reference_ids"]).issubset(rule_keys))
            applicable = set()
            for rule in rules:
                scope = rule["scope"]
                same_scope = all(scope[key] == context[key]
                                 for key in ("study_id", "site_id", "protocol_id"))
                in_time = datetime.fromisoformat(scope["effective_from"]) <= event
                if scope["effective_to"]:
                    in_time = in_time and event < datetime.fromisoformat(scope["effective_to"])
                if same_scope and in_time:
                    applicable.add(f"{rule['rule_id']}@{rule['version']}")
            self.assertTrue(set(query["reference_ids"]).issubset(applicable), query["query_id"])
            if not query["reference_ids"]:
                no_target.append(query)
                self.assertEqual(applicable, set())
            if context["protocol_id"] == "SYN-PROTOCOL-001-v2":
                self.assertEqual(applicable, {"SYN-RULE-PK-001@2"})
                self.assertNotIn("SYN-RULE-PK-001@1", applicable)
        self.assertEqual(len(no_target), 1)
        self.assertEqual(no_target[0]["split"], "eval")
        self.assertEqual(sum(len(query["reference_ids"]) > 1 for query in queries), 3)


if __name__ == "__main__":
    unittest.main()
