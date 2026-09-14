"""Offline mathematical checks using explicit vectors, never model substitutes."""
from copy import deepcopy
import builtins
import io
import math
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinical_qc_demo.hybrid_retrieval import apply_reranker, build_comparison


class HybridRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.documents = [{"id": "A", "text": "needle needle sample"},
                          {"id": "B", "text": "specimen draw"},
                          {"id": "C", "text": "other"}]
        self.query_vector = [1.0, 0.0]
        self.vectors = {"A": [0.0, 1.0], "B": [1.0, 0.0], "C": [-1.0, 0.0]}

    def compare(self, **kwargs):
        return build_comparison("needle", self.documents, self.query_vector, self.vectors, **kwargs)

    @staticmethod
    def ids(ranking):
        return [row["document_id"] for row in ranking]

    def test_lexical_and_supplied_semantic_vectors_disagree(self):
        result = self.compare()
        self.assertEqual(self.ids(result["rankings"]["bm25"]), ["A", "B", "C"])
        self.assertEqual(self.ids(result["rankings"]["dense"]), ["B", "A", "C"])
        self.assertEqual([row["score"] for row in result["rankings"]["dense"]], [1.0, 0.0, -1.0])
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["reranker_status"], "not_run")
        self.assertEqual(result["version"], "retrieval-v2")
        self.assertEqual(result["document_count"], 3)

    def test_exact_bm25_equation_binary_query_and_chinese_tokenization(self):
        documents = [{"id": "A", "text": "PK PK 样本"}, {"id": "B", "text": "other"}]
        vectors = {"A": [1.0], "B": [1.0]}
        result = build_comparison("PK", documents, [1.0], vectors)
        expected = math.log1p(1.5 / 1.5) * 2 * 2.5 / (2 + 1.5 * (0.25 + 0.75 * 3 / 2))
        self.assertAlmostEqual(result["rankings"]["bm25"][0]["score"], expected, places=14)
        self.assertEqual(result, build_comparison("PK PK", documents, [1.0], vectors))
        chinese = build_comparison("样本", documents, [1.0], vectors)
        self.assertGreater(chinese["rankings"]["bm25"][0]["score"], 0.0)

    def test_rrf_equation_one_based_ranks_and_id_ties(self):
        result = self.compare(rrf_k=5)
        self.assertEqual(self.ids(result["rankings"]["hybrid"]), ["A", "B", "C"])
        scores = {row["document_id"]: row["score"] for row in result["rankings"]["hybrid"]}
        self.assertAlmostEqual(scores["A"], 1 / 6 + 1 / 7, places=14)
        self.assertEqual(scores["A"], scores["B"])
        self.assertAlmostEqual(scores["C"], 2 / 8, places=14)
        self.assertEqual(result, build_comparison("needle", list(reversed(self.documents)),
                                                 self.query_vector, dict(reversed(list(self.vectors.items()))),
                                                 rrf_k=5))

    def test_candidate_cutoff_does_not_truncate_rankings(self):
        documents = [{"id": f"D{index:02}", "text": "needle"} for index in range(12)]
        vectors = {document["id"]: [1.0] for document in documents}
        result = build_comparison("needle", documents, [1.0], vectors, candidate_k=8, final_k=4)
        self.assertEqual(result["document_count"], 12)
        for ranking in result["rankings"].values():
            self.assertEqual(len(ranking), 12)
            self.assertEqual([row["rank"] for row in ranking], list(range(1, 13)))
        self.assertEqual(result["candidate_ids"], [f"D{index:02}" for index in range(8)])
        self.assertEqual(result["selected_ids"], [f"D{index:02}" for index in range(4)])
        scores = {document_id: float(index) for index, document_id in enumerate(result["candidate_ids"])}
        reranked = apply_reranker(result, scores)
        self.assertEqual(len(reranked["rankings"]["hybrid_reranked"]), 8)
        self.assertEqual(reranked["selected_ids"], ["D07", "D06", "D05", "D04"])
        self.assertEqual(reranked["rankings"]["bm25"], result["rankings"]["bm25"])

    def test_zero_lexical_scores_and_empty_document_texts_remain_ranked(self):
        documents = [{"id": "B", "text": "！！！"}, {"id": "A", "text": ""}]
        result = build_comparison("", documents, [1.0], {"B": [1.0], "A": [1.0]})
        self.assertEqual(self.ids(result["rankings"]["bm25"]), ["A", "B"])
        self.assertEqual([row["score"] for row in result["rankings"]["bm25"]], [0.0, 0.0])
        self.assertEqual(result["selected_ids"], ["A", "B"])

    def test_empty_corpus(self):
        result = build_comparison("needle", [], [1.0], {})
        self.assertEqual(result["document_count"], 0)
        self.assertEqual(result["candidate_ids"], [])
        self.assertEqual(result["selected_ids"], [])
        self.assertTrue(all(ranking == [] for ranking in result["rankings"].values()))
        self.assertEqual(apply_reranker(result, {})["rankings"]["hybrid_reranked"], [])
        with self.assertRaises(ValueError):
            build_comparison("needle", [], [], {})
        with self.assertRaises(ValueError):
            build_comparison("needle", [], [1.0], {"unknown": [1.0]})

    def test_vector_scaling_and_large_small_finite_values(self):
        documents = [{"id": "A", "text": "a"}]
        for scale in (1.0, 1e308, 1e-308):
            with self.subTest(scale=scale):
                result = build_comparison("a", documents, [scale, scale], {"A": [scale, scale]})
                self.assertAlmostEqual(result["rankings"]["dense"][0]["score"], 1.0, places=14)
        result = build_comparison("a", documents, [1.0, 2.0], {"A": [3.0, 4.0]})
        self.assertAlmostEqual(result["rankings"]["dense"][0]["score"], 11 / math.sqrt(125), places=14)

    def test_invalid_query_and_document_vectors(self):
        invalid = ([], [0.0, 0.0], [float("nan"), 1.0], [float("inf"), 1.0],
                   [float("-inf"), 1.0], [True, 1.0], ["1", 1.0], [1j, 1.0],
                   None, "1,0", [10 ** 1000, 1.0])
        for vector in invalid:
            with self.subTest(vector_type=type(vector).__name__), self.assertRaises(ValueError):
                build_comparison("needle", self.documents, vector, self.vectors)
            with self.subTest(document_vector_type=type(vector).__name__), self.assertRaises(ValueError):
                build_comparison("needle", self.documents, self.query_vector, {**self.vectors, "A": vector})
        with self.assertRaises(ValueError):
            build_comparison("needle", self.documents, self.query_vector, {**self.vectors, "A": [1.0]})

    def test_duplicate_missing_unknown_and_invalid_document_ids(self):
        with self.assertRaises(ValueError):
            build_comparison("needle", self.documents + [self.documents[0]], self.query_vector, self.vectors)
        for vectors in ({"A": [1.0, 0.0]}, {**self.vectors, "unknown": [1.0, 0.0]}, []):
            with self.subTest(vectors=vectors), self.assertRaises(ValueError):
                build_comparison("needle", self.documents, self.query_vector, vectors)
        for document in ({"id": "", "text": "x"}, {"id": 1, "text": "x"},
                         {"id": "A"}, {"id": "A", "text": None}, "A"):
            with self.subTest(document=document), self.assertRaises(ValueError):
                build_comparison("needle", [document], [1.0], {"A": [1.0]})
        with self.assertRaises(TypeError):
            build_comparison(None, [], [1.0], {})

    def test_positive_bounded_cutoffs(self):
        for name in ("candidate_k", "final_k", "rrf_k"):
            for value in (True, False, 0, -1, 1.0, "8", float("inf"), 100001):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.compare(**{name: value})
        for name in ("candidate_k", "final_k"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.compare(**{name: 1001})
        with self.assertRaises(ValueError):
            self.compare(candidate_k=2, final_k=3)
        self.assertEqual(self.compare(candidate_k=1, final_k=1)["selected_ids"], ["A"])

    def test_reranker_scores_order_candidates_and_id_ties(self):
        comparison = self.compare(candidate_k=3, final_k=2)
        scores = {"B": -5.0, "A": -5.0, "C": 17.5}
        result = apply_reranker(comparison, scores)
        self.assertEqual(self.ids(result["rankings"]["hybrid_reranked"]), ["C", "A", "B"])
        self.assertEqual([row["score"] for row in result["rankings"]["hybrid_reranked"]], [17.5, -5.0, -5.0])
        self.assertEqual(result["selected_ids"], ["C", "A"])
        self.assertEqual(result["candidate_ids"], comparison["candidate_ids"])
        self.assertEqual(result["mode"], "hybrid_reranked")
        self.assertEqual(result["reranker_status"], "completed")
        for name in ("bm25", "dense", "hybrid"):
            self.assertEqual(result["rankings"][name], comparison["rankings"][name])

    def test_reranker_requires_exact_candidate_set_and_finite_real_scores(self):
        comparison = self.compare(candidate_k=2, final_k=1)
        for scores in ({}, {"A": 1.0}, {"A": 1.0, "C": 2.0},
                       {"A": 1.0, "B": 2.0, "C": 3.0}, [], None):
            with self.subTest(scores=scores), self.assertRaises(ValueError):
                apply_reranker(comparison, scores)
        for value in (True, False, float("nan"), float("inf"), float("-inf"), "1", 1j, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply_reranker(comparison, {"A": value, "B": 1.0})

    def test_no_input_mutation_or_file_network_io(self):
        original = deepcopy((self.documents, self.query_vector, self.vectors))
        with patch.object(Path, "read_text", side_effect=AssertionError("No file reads")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("No file reads")), \
                patch.object(builtins, "open", side_effect=AssertionError("No file reads")), \
                patch.object(io, "open", side_effect=AssertionError("No file reads")), \
                patch.object(socket, "socket", side_effect=AssertionError("No network/model calls")):
            comparison = self.compare()
            before = deepcopy(comparison)
            scores = {"A": 1.0, "B": 2.0, "C": 3.0}
            reranked = apply_reranker(comparison, scores)
        self.assertEqual((self.documents, self.query_vector, self.vectors), original)
        self.assertEqual(comparison, before)
        self.assertEqual(scores, {"A": 1.0, "B": 2.0, "C": 3.0})
        reranked["rankings"]["bm25"][0]["score"] = 999.0
        reranked["candidate_ids"].clear()
        reranked["config"]["final_k"] = 99
        self.assertEqual(comparison, before)


if __name__ == "__main__":
    unittest.main()
