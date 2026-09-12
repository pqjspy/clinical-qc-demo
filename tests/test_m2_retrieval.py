"""Offline retrieval tests; these do not read authored reference answers."""
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

from clinical_qc_demo.contracts import CaseRecord, Rule
from clinical_qc_demo.data import load_knowledge, load_records
from clinical_qc_demo.retrieval import retrieve_rules, tokenize


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only input and knowledge readers are allowed; no reference reader.
        _, _, knowledge = load_knowledge(ROOT)
        cls.rules = knowledge.rules
        cls.records = {record.case_id: record for record in load_records(ROOT)}

    def scoped_rule(self, key: str, quote: str, **updates) -> Rule:
        item = self.rules[0].model_dump()
        item["rule_id"] = key
        item["source"]["quote"] = quote
        item.update(updates)
        return Rule.model_validate(item)

    def record_with(self, **updates) -> CaseRecord:
        item = self.records["QC002"].model_dump()
        item.update(updates)
        return CaseRecord.model_validate(item)

    def test_tokenizer_chinese_ascii_and_punctuation(self):
        self.assertEqual(tokenize("PK 样本处理，EDC urgent_flag 30分钟"),
                         ["pk", "样本", "本处", "处理", "edc", "urgent_flag", "30", "分钟"])
        self.assertEqual(tokenize("采血，离心 A-B 中"), ["采血", "离心", "a", "b"])
        self.assertEqual(tokenize("A A"), ["a", "a"])
        self.assertEqual(tokenize("！！"), [])
        with self.assertRaises(TypeError):
            tokenize(None)

    def test_single_document_manual_bm25(self):
        rule = self.scoped_rule("ONE", "PK PK 样本")
        result = retrieve_rules(self.records["QC002"], "pk", [rule])
        # N=df=1, dl=avgdl=3; tf=2; k1=1.5; b=.75.
        expected = math.log(1 + 0.5 / 1.5) * (2 * 2.5) / (2 + 1.5)
        self.assertAlmostEqual(result["ranking"][0]["score"], expected, places=14)
        self.assertEqual(result["ranking"][0]["matched_terms"], ["pk"])
        self.assertEqual(result["configuration"]["average_document_token_count"], 3)

    def test_two_documents_idf_and_length_manual(self):
        rules = [self.scoped_rule("A", "pk pk sample"),
                 self.scoped_rule("B", "other")]
        result = retrieve_rules(self.records["QC002"], "PK", rules)
        expected = math.log(1 + 1.5 / 1.5) * (2 * 2.5) / (2 + 1.5 * (0.25 + 0.75 * 3 / 2))
        self.assertAlmostEqual(result["ranking"][0]["score"], expected, places=14)
        self.assertEqual(result["ranking"][1]["score"], 0)
        self.assertEqual(result["candidate_rule_keys"], ["A@1"])

    def test_query_duplicate_terms_do_not_change_scores(self):
        simple = retrieve_rules(self.records["QC002"], "pk 样本", self.rules)
        repeated = retrieve_rules(self.records["QC002"], "pk pk 样本 样本", self.rules)
        self.assertEqual(simple["ranking"], repeated["ranking"])

    def test_zero_score_and_empty_corpus(self):
        result = retrieve_rules(self.records["QC002"], "zyzzyva", self.rules)
        self.assertEqual(result["candidate_rule_keys"], [])
        self.assertEqual(len(result["ranking"]), 6)
        self.assertTrue(all(item["score"] == 0 and not item["matched_terms"] for item in result["ranking"]))
        empty = retrieve_rules(self.records["QC002"], "样本", [])
        self.assertEqual(empty["ranking"], [])
        self.assertEqual(empty["applicable_rule_keys"], [])
        self.assertEqual(empty["conflicts"], [])
        self.assertEqual(empty["configuration"]["average_document_token_count"], 0)

    def test_empty_query_and_zero_length_documents(self):
        rules = [self.scoped_rule("EMPTY", "！！！")]
        for query in ("", " ", "采血"):
            result = retrieve_rules(self.records["QC002"], query, rules)
            self.assertEqual(result["ranking"][0]["score"], 0)
            self.assertEqual(result["candidate_rule_keys"], [])

    def test_stable_ties_and_no_top_k(self):
        rules = [self.scoped_rule("RULE-" + str(number), "样本") for number in range(8)]
        result = retrieve_rules(self.records["QC002"], "样本", rules)
        reverse = retrieve_rules(self.records["QC002"], "样本", list(reversed(rules)))
        self.assertEqual(result, reverse)
        self.assertEqual(result["candidate_rule_keys"], sorted(rule.key for rule in rules))
        self.assertEqual(len(result["candidate_rule_keys"]), 8)

    def test_scope_filtering_precedes_document_statistics(self):
        base = self.scoped_rule("BASE", "pk")
        other_scope = self.rules[-1].model_dump()["scope"]
        outside = self.scoped_rule("OUTSIDE", "pk " * 1000, scope=other_scope)
        only = retrieve_rules(self.records["QC002"], "pk", [base])
        mixed = retrieve_rules(self.records["QC002"], "pk", [base, outside])
        self.assertEqual(only, mixed)
        for updates in ({"study_id": "SYN-NOT-FOUND"}, {"site_id": "SYN-SITE-B"},
                        {"protocol_id": "SYN-NOT-FOUND"}):
            self.assertEqual(retrieve_rules(self.record_with(**updates), "pk", self.rules)["applicable_rule_keys"], [])

    def test_v1_and_v2_half_open_boundaries_and_override(self):
        old = self.record_with(protocol_id="SYN-PROTOCOL-001-v1")
        new = self.record_with(protocol_id="SYN-PROTOCOL-001-v2")
        before = "2026-09-30T23:59:59+08:00"
        boundary = "2026-10-01T00:00:00+08:00"
        self.assertEqual(len(retrieve_rules(old, "样本", self.rules, event_at=before)["applicable_rule_keys"]), 6)
        self.assertEqual(retrieve_rules(old, "样本", self.rules, event_at=boundary)["applicable_rule_keys"], [])
        self.assertEqual(retrieve_rules(new, "样本", self.rules, event_at=before)["applicable_rule_keys"], [])
        self.assertEqual(retrieve_rules(new, "样本", self.rules, event_at=boundary)["applicable_rule_keys"], ["SYN-RULE-PK-001@2"])
        equivalent = "2026-09-30T16:00:00+00:00"
        self.assertEqual(retrieve_rules(new, "样本", self.rules, event_at=equivalent)["applicable_rule_keys"], ["SYN-RULE-PK-001@2"])
        self.assertEqual(new.event_at, self.records["QC002"].event_at)

    def test_naive_or_invalid_event_override_rejected_even_if_no_rules(self):
        for event in ("2026-09-01T10:33:00", "", "not-a-time"):
            with self.assertRaises(ValueError):
                retrieve_rules(self.records["QC002"], "样本", [], event_at=event)

    def test_conflicts_survive_zero_score_and_unequal_ranking(self):
        record = self.records["DEMO-CONFLICT-01"]
        expected = [["SYN-RULE-PK-CONFLICT-A@1", "SYN-RULE-PK-CONFLICT-B@1"]]
        zero = retrieve_rules(record, "zyzzyva", self.rules)
        self.assertEqual(zero["conflicts"], expected)
        self.assertEqual(zero["candidate_rule_keys"], [])
        self.assertEqual(len(zero["applicable_rule_keys"]), 2)
        ranked = retrieve_rules(record, "30", self.rules)
        self.assertEqual(ranked["candidate_rule_keys"], ["SYN-RULE-PK-CONFLICT-A@1"])
        self.assertEqual(ranked["conflicts"], expected)
        self.assertEqual(ranked, retrieve_rules(record, "30", list(reversed(self.rules))))

    def test_raw_quotes_are_entire_document_unit_not_ids_or_answer(self):
        rule = self.scoped_rule("UNIQUEIDENTIFIER", "真实原文XYZTOKEN")
        result = retrieve_rules(self.records["QC002"], "xyztoken", [rule])
        self.assertGreater(result["ranking"][0]["score"], 0)
        self.assertEqual(retrieve_rules(self.records["QC002"], "uniqueidentifier", [rule])["candidate_rule_keys"], [])

    def test_duplicates_rejected_no_mutation_no_file_or_network_io(self):
        with self.assertRaises(ValueError):
            retrieve_rules(self.records["QC002"], "pk", [self.rules[0], self.rules[0]])
        record_before = self.records["QC002"].model_dump()
        rules_before = [deepcopy(rule.model_dump()) for rule in self.rules]
        with patch.object(Path, "read_text", side_effect=AssertionError("No file reads")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("No file reads")), \
                patch.object(builtins, "open", side_effect=AssertionError("No answer/file reads")), \
                patch.object(io, "open", side_effect=AssertionError("No file reads")), \
                patch.object(socket, "socket", side_effect=AssertionError("No network/model calls")):
            result = retrieve_rules(self.records["QC002"], "PK样本采血开始离心时间", self.rules)
        self.assertEqual(result["ranking"][0]["rule_key"], "SYN-RULE-PK-001@1")
        self.assertEqual(self.records["QC002"].model_dump(), record_before)
        self.assertEqual([rule.model_dump() for rule in self.rules], rules_before)
        self.assertIn("not_confidence", result["configuration"]["score_meaning"])


if __name__ == "__main__":
    unittest.main()
